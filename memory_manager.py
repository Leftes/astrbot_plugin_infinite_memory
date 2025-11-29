import json
import time
import os
import sqlite3
import re
from typing import Optional, List, Dict, Any
from astrbot.api import logger
from .memory_db import MemoryDB
from astrbot.api.event import AstrMessageEvent


class MemoryManager:
    def __init__(self, context, config, data_dir: str):
        self.context = context
        self.config = config
        self.data_dir = data_dir
        logger.debug(f"MemoryManager 使用数据目录: {self.data_dir}")

    def _get_db_path(self, event) -> str:
        group_isolation = self.config.get("group_isolation", True)
        if group_isolation:
            identifier = getattr(event.message_obj, 'group_id', None) or event.unified_msg_origin
        else:
            identifier = "global"
        return os.path.join(self.data_dir, f"memories_{identifier}.db")

    async def store_source_summary(self, event, summary_text: str) -> int:
        try:
            db_path = self._get_db_path(event)
            with MemoryDB(db_path) as db:
                token_count = len(summary_text) // 2
                summary_id = db.save_source_summary(summary_text, token_count)
                logger.debug(f"💾 原始总结已存储 (ID: {summary_id}, DB: {os.path.basename(db_path)})")
                return summary_id
        except Exception as e:
            logger.error(f"存储原始总结失败: {e}", exc_info=True)
            return -1

    async def inject_memory(self, event, summary_text: str, source_summary_id: int):
        try:
            db_path = self._get_db_path(event)
            memory_text = await self._generate_memory_text(summary_text, event)
            keywords = await self._extract_keywords(summary_text)

            # ✅ 支持 embedding_provider_id
            embedding = None
            use_embedding = self.config.get("use_embedding", True)
            embedding_provider_id = self.config.get("embedding_provider_id", "")
            if use_embedding and hasattr(self.context, 'text_embedding'):
                try:
                    if embedding_provider_id:
                        embedding = await self.context.text_embedding(
                            text=memory_text,
                            provider_id=embedding_provider_id
                        )
                    else:
                        embedding = await self.context.text_embedding(memory_text)
                except Exception as e:
                    logger.warning(f"embedding 生成失败，回退: {e}")

            with MemoryDB(db_path) as db:
                memory_id = db.save_memory(
                    summary_id=source_summary_id,
                    summary=memory_text,
                    keywords=keywords,
                    weight=1.0,
                    embedding=embedding
                )

                # ✅ 修正：获取真实 user_id
                if hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'user_id'):
                    trigger_user_id = str(event.message_obj.sender.user_id).strip()
                else:
                    trigger_user_id = event.unified_msg_origin

                # ✅ 解析多用户（基于 LLM 输出）
                participants = self._extract_participants(summary_text)
                high_relevance_users = [
                    p for p in participants 
                    if p.get("relevance") in ["核心", "活跃"]
                ]
                # 至少包含触发者（兜底）
                if not high_relevance_users:
                    high_relevance_users = [{
                        "user_id": trigger_user_id,
                        "display_names": [getattr(event.message_obj.sender, 'nickname', 'user')],
                        "role": "用户",
                        "relevance": "核心"
                    }]

                logger.info(f"📊 识别 {len(participants)} 位用户，仅更新 {len(high_relevance_users)} 位高相关用户画像")

                for p in high_relevance_users:
                    # ✅ 关键：使用纯 user_id（非 session_id）
                    user_id = str(p["user_id"]).strip()
                    display_name = p["display_names"][0] if p["display_names"] else "unknown"
                    
                    profile = db.get_or_create_user_profile(user_id, display_name)
                    
                    # 更新多称呼
                    if len(p["display_names"]) > 1:
                        current_names = json.loads(profile.get('display_names', '[]'))
                        for name in p["display_names"]:
                            if name not in current_names:
                                current_names.append(name)
                        with sqlite3.connect(db_path) as conn:
                            cursor = conn.cursor()
                            cursor.execute(
                                "UPDATE user_profiles SET display_names = ? WHERE user_id = ?",
                                (json.dumps(current_names), user_id)
                            )
                            conn.commit()

                    affinity_change = self._analyze_affinity_for_user(summary_text, user_id)
                    traits = self._analyze_traits_for_user(summary_text, user_id)
                    
                    db.update_user_profile(
                        user_id=user_id,
                        summary=memory_text[:100],
                        affinity_change=affinity_change,
                        traits=traits,
                        embedding=embedding
                    )

                    # 创建双向连接
                    db.create_connection(
                        from_id=profile['id'],
                        to_id=memory_id,
                        conn_type="user",
                        strength=0.8,
                        direction="forward",
                        keywords=["participant", p["role"]]
                    )
                    db.create_connection(
                        from_id=memory_id,
                        to_id=profile['id'],
                        conn_type="user",
                        strength=0.6,
                        direction="backward",
                        keywords=["context", p["role"]]
                    )

            logger.info(f"🧠 记忆注入成功：{len(high_relevance_users)} 位用户，记忆ID={memory_id}")
        except Exception as e:
            logger.error(f"记忆注入失败: {e}", exc_info=True)

    async def _generate_memory_text(self, summary_text: str, event: AstrMessageEvent) -> str:
        clean_text = summary_text.replace("【前情提要】", "").strip()
        if len(clean_text) <= 150:
            return clean_text
        prompt = (
            "你是一个记忆压缩专家，将对话总结提炼为≤150字核心记忆。\n"
            "要求：1.保留人物/决策/任务/特征 2.删修饰语 3.客观语气 4.直接输出\n\n"
            f"原文：{clean_text}\n\n精简记忆："
        )
        max_retries = self.config.get("max_retries", 3)
        use_target_provider = self.config.get("summary_provider_id", "")
        uid = event.unified_msg_origin
        for i in range(max_retries):
            try:
                current_pid = await self.context.get_current_chat_provider_id(umo=uid)
                provider_id = use_target_provider or current_pid
                resp = await self.context.llm_generate(chat_provider_id=provider_id, prompt=prompt)
                if resp and resp.completion_text:
                    text = resp.completion_text.strip()
                    return text[:150]
            except Exception as e:
                logger.warning(f"记忆压缩尝试 {i+1}/{max_retries} 失败: {e}")
                use_target_provider = ""
        logger.warning("⚠️ LLM 压缩失败，回退简单截断")
        return clean_text[:147] + "..."

    async def _extract_keywords(self, summary_text: str) -> List[str]:
        sentences = [s.strip() for s in summary_text.replace('。', '，').split('，') if s.strip()]
        return sentences[:5]

    def _extract_participants(self, summary_text: str) -> List[Dict]:
        """解析 LLM 输出的用户列表"""
        participants = []
        # 匹配：十一（ID: 2980223165，角色：前辈大魔女，相关度：【核心】）
        pattern = r'(\S+?)\s*\(ID:\s*(\d+),\s*角色：([^，)]+),\s*相关度：【([^】]+)】\)'
        matches = re.findall(pattern, summary_text)
        for name, user_id, role, relevance in matches:
            names = [n.strip() for n in name.replace("/", "、").split("、") if n.strip()]
            participants.append({
                "user_id": user_id.strip(),  # ✅ 纯数字ID
                "display_names": names,
                "role": role,
                "relevance": relevance
            })
        return participants

    def _analyze_affinity_for_user(self, summary_text: str, user_id: str) -> int:
        if user_id in summary_text:
            positive_words = ['喜欢', '开心', '满意', '感谢', '棒', '厉害', '优秀']
            negative_words = ['讨厌', '失望', '差', '垃圾', '不行', '不好']
            for word in positive_words:
                if word in summary_text:
                    return 5
            for word in negative_words:
                if word in summary_text:
                    return -5
        return 0

    def _analyze_traits_for_user(self, summary_text: str, user_id: str) -> Dict[str, str]:
        traits = {}
        if user_id in summary_text:
            if any(kw in summary_text for kw in ['计划', '安排', '准备', '打算']):
                traits['planning'] = 'active'
            if any(kw in summary_text for kw in ['帮助', '支持', '协助', '请教']):
                traits['helpful'] = 'high'
        return traits

    async def recall_relevant_memories(self, event: AstrMessageEvent, current_text: str) -> str:
        try:
            db_path = self._get_db_path(event)
            if not os.path.exists(db_path):
                return ""
            with MemoryDB(db_path) as db:
                with sqlite3.connect(db_path) as conn:
                    conn.row_factory = sqlite3.Row
                    cursor = conn.cursor()
                    cursor.execute("""
                        SELECT id, summary, timestamp, weight 
                        FROM memories 
                        WHERE summary LIKE ? OR keywords LIKE ?
                        ORDER BY timestamp DESC, weight DESC
                        LIMIT 3
                    """, (f"%{current_text}%", f"%{current_text}%"))
                    keyword_memories = [dict(r) for r in cursor.fetchall()]
                # 用户最近记忆（✅ 使用真实 user_id）
                if hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'user_id'):
                    user_id = str(event.message_obj.sender.user_id).strip()
                else:
                    user_id = event.unified_msg_origin
                profile = db.get_or_create_user_profile(user_id)
                if profile:
                    cursor.execute("""
                        SELECT m.id, m.summary, m.timestamp 
                        FROM memories m
                        JOIN connections c ON m.id = c.to_id
                        WHERE c.from_id = ? AND c.type = 'user' AND c.direction = 'forward'
                        ORDER BY m.timestamp DESC
                        LIMIT 2
                    """, (profile['id'],))
                    user_memories = [dict(r) for r in cursor.fetchall()]
                else:
                    user_memories = []
                all_mems = {m['id']: m for m in keyword_memories + user_memories}.values()
                sorted_mems = sorted(all_mems, key=lambda x: x['timestamp'], reverse=True)[:5]
                if not sorted_mems:
                    return ""
                mem_texts = "\n".join([f"[{m['summary']}]" for m in sorted_mems])
                prompt = (
                    "将以下记忆整合为≤200字背景描述，用于AI对话。\n"
                    "要求：1.保留关键事实 2.以'根据记忆'开头 3.客观语气\n\n"
                    f"记忆：\n{mem_texts}\n\n整合结果："
                )
                uid = event.unified_msg_origin
                current_pid = await self.context.get_current_chat_provider_id(umo=uid)
                resp = await self.context.llm_generate(chat_provider_id=current_pid, prompt=prompt)
                if resp and resp.completion_text:
                    return resp.completion_text.strip()[:200]
        except Exception as e:
            logger.error(f"记忆召回失败: {e}", exc_info=True)
        return ""