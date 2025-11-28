import json
import time
import os
import sqlite3
from typing import Optional, List, Dict, Any
from astrbot.api import logger
from .memory_db import MemoryDB
from astrbot.api.event import AstrMessageEvent


class MemoryManager:
    def __init__(self, context, config, data_dir: str):
        """修正：接收主插件传入的 data_dir，不再自行拼 path"""
        self.context = context
        self.config = config
        self.data_dir = data_dir  # 由 main.py 传入 StarTools.get_data_dir()
        logger.debug(f"MemoryManager 使用数据目录: {self.data_dir}")

    def _get_db_path(self, event) -> str:
        """根据群隔离设置获取数据库路径"""
        group_isolation = self.config.get("group_isolation", True)
        if group_isolation:
            identifier = getattr(event.message_obj, 'group_id', None) or event.unified_msg_origin
        else:
            identifier = "global"
        return os.path.join(self.data_dir, f"memories_{identifier}.db")

    async def store_source_summary(self, event, summary_text: str) -> int:
        """存储原始总结"""
        try:
            db_path = self._get_db_path(event)
            with MemoryDB(db_path) as db:
                token_count = len(summary_text) // 2  # 粗略估算
                summary_id = db.save_source_summary(summary_text, token_count)
                logger.debug(f"💾 原始总结已存储 (ID: {summary_id}, DB: {os.path.basename(db_path)})")
                return summary_id
        except Exception as e:
            logger.error(f"存储原始总结失败: {e}", exc_info=True)
            return -1

    async def inject_memory(self, event, summary_text: str, source_summary_id: int):
        """注入记忆：基于总结生成记忆和用户画像（第二阶段核心）"""
        try:
            db_path = self._get_db_path(event)
            #LLM 智能压缩精简记忆
            memory_text = await self._generate_memory_text(summary_text, event)
            keywords = await self._extract_keywords(summary_text)

            #修正：embedding 兼容性检查（AstrBot 4.x 为 text_embedding）
            embedding = None
            use_embedding = self.config.get("use_embedding", True)
            if use_embedding and hasattr(self.context, 'text_embedding'):
                try:
                    embedding_provider_id = self.config.get("embedding_provider_id", "")
                    if embedding_provider_id:
                        try:
                            embedding = await self.context.text_embedding(
                                text=memory_text,
                                provider_id=embedding_provider_id  # ← 关键参数
                            )
                        except Exception as e:
                            logger.warning(f"使用指定 embedding_provider_id '{embedding_provider_id}' 失败，回退默认: {e}")
                            embedding = await self.context.text_embedding(memory_text)
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

                # 更新用户画像
                user_id = event.unified_msg_origin
                display_name = None
                if hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'nickname'):
                    display_name = event.message_obj.sender.nickname

                profile = db.get_or_create_user_profile(user_id, display_name)
                affinity_change, traits = await self._analyze_summary_for_profile(summary_text)
                db.update_user_profile(
                    user_id=user_id,
                    summary=memory_text[:100],  # 截取前100字
                    affinity_change=affinity_change,
                    traits=traits,
                    embedding=embedding
                )

                # 创建双向连接（ 方向分离）
                self._create_connections(db, memory_id, user_id, profile['id'])

            logger.info(f"🧠 记忆注入成功: 记忆ID={memory_id}, 用户ID={user_id}")
        except Exception as e:
            logger.error(f"记忆注入失败: {e}", exc_info=True)

    # ========== 第二阶段核心：LLM 智能精简记忆 ==========
    async def _generate_memory_text(self, summary_text: str, event: AstrMessageEvent) -> str:
        """500字 → ≤150字核心记忆（✅ 修正 API）"""
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
                #  修正：get_current_chat_provider_id(umo=uid)
                current_pid = await self.context.get_current_chat_provider_id(umo=uid)
                provider_id = use_target_provider or current_pid

                resp = await self.context.llm_generate(
                    chat_provider_id=provider_id,
                    prompt=prompt
                )
                if resp and resp.completion_text:
                    text = resp.completion_text.strip()
                    return text[:150]  # 严格兜底
            except Exception as e:
                logger.warning(f"记忆压缩尝试 {i+1}/{max_retries} 失败: {e}")
                use_target_provider = ""

        logger.warning("⚠️ LLM 压缩失败，回退简单截断")
        return clean_text[:147] + "..."

    async def _extract_keywords(self, summary_text: str) -> List[str]:
        """提取关键词"""
        prompt = f"提取5个核心关键词（逗号分隔）：{summary_text}"
        try:
            uid = getattr(event, 'unified_msg_origin', None)
            current_pid = await self.context.get_current_chat_provider_id(umo=uid) if uid else None
            resp = await self.context.llm_generate(
                chat_provider_id=current_pid or self.config.get("summary_provider_id", ""),
                prompt=prompt
            )
            if resp and resp.completion_text:
                keywords = [k.strip() for k in resp.completion_text.split(',') if k.strip()]
                return keywords[:5]
        except:
            pass
        # 回退：简单规则
        sentences = [s.strip() for s in summary_text.replace('。', '，').split('，') if s.strip()]
        return sentences[:5]

    async def _analyze_summary_for_profile(self, summary_text: str) -> (int, Dict[str, Any]):
        """分析总结更新用户画像"""
        # 简化版（避免 JSON 解析失败）
        positive_words = ['喜欢', '开心', '满意', '感谢', '棒', '厉害', '优秀', '赞', '好']
        negative_words = ['讨厌', '失望', '差', '垃圾', '不行', '不好', '烦', '无聊']
        affinity_change = sum(5 for w in positive_words if w in summary_text)
        affinity_change -= sum(5 for w in negative_words if w in summary_text)
        affinity_change = max(-10, min(10, affinity_change))  # 单次变化限制

        traits = {}
        if any(kw in summary_text for kw in ['计划', '安排', '准备', '打算']):
            traits['planning'] = 'active'
        if any(kw in summary_text for kw in ['帮助', '支持', '协助', '请教']):
            traits['helpful'] = 'high'
        return affinity_change, traits

    def _create_connections(self, db: MemoryDB, memory_id: int, user_id: str, profile_id: int):
        """创建双向连接（独立记录）"""
        db.create_connection(
            from_id=profile_id,
            to_id=memory_id,
            conn_type="user",
            strength=0.8,
            direction="forward",  # 用户→记忆
            keywords=["interaction", "generated"]
        )
        db.create_connection(
            from_id=memory_id,
            to_id=profile_id,
            conn_type="user",
            strength=0.6,
            direction="backward",  # 记忆→用户
            keywords=["source", "context"]
        )
        logger.debug("🔗 连接关系已创建（双向独立）")

    # ========== 第二阶段新增：记忆召回 ==========
    async def recall_relevant_memories(self, event: AstrMessageEvent, current_text: str) -> str:
        """召回并压缩相关记忆（≤200字）"""
        try:
            db_path = self._get_db_path(event)
            if not os.path.exists(db_path):
                return ""

            with MemoryDB(db_path) as db:
                # 1. 关键词模糊搜索
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

                # 2. 当前用户的最近记忆
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

                # 3. 合并去重
                all_mems = {m['id']: m for m in keyword_memories + user_memories}.values()
                sorted_mems = sorted(all_mems, key=lambda x: x['timestamp'], reverse=True)[:5]

                if not sorted_mems:
                    return ""

                # 4. LLM 压缩为单一上下文
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