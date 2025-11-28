from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.core.message.components import Plain
import json
import asyncio
import os
import time
import sqlite3
from .memory_manager import MemoryManager


@register(
    "astrbot_plugin_infinite_memory", 
    "Alan Backer", 
    "无限记忆插件：自动总结对话历史，构建记忆图谱", 
    "1.1.0", 
    "https://github.com/AlanBacker/astrbot_plugin_infinite_memory"
)
class InfiniteMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.memory_manager = MemoryManager(context, config, self.data_dir)
        logger.info(f"🧠 无限记忆插件 启动，数据目录: {self.data_dir}")

    async def initialize(self):
        pass

    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent, *args, **kwargs):
        if not self._is_event_valid(event):
            logger.warning("无效的事件对象")
            return

        if not self._check_whitelist(event):
            return

        conversation = await self._get_conversation(event)
        if not conversation or not conversation.history:
            return

        try:
            if self._should_summarize(conversation):
                summary_text = await self._generate_summary(conversation, event)
                if summary_text:
                    source_summary_id = await self.memory_manager.store_source_summary(
                        event, 
                        summary_text
                    )
                    await self.memory_manager.inject_memory(
                        event, 
                        summary_text, 
                        source_summary_id
                    )
                    await self._apply_summary_with_tail(
                        event, 
                        conversation, 
                        summary_text,
                        source_summary_id
                    )
        except json.JSONDecodeError as e:
            logger.error(f"JSON解析失败: {e}")
        except Exception as e:
            logger.error(f"处理消息时发生错误: {e}", exc_info=True)

    # ========== 记忆召回钩子（优先级 90） ==========
    @filter.event_message_type(filter.EventMessageType.ALL, priority=90)
    async def on_recall_memory(self, event: AstrMessageEvent):
        """在 LLM 请求前注入相关记忆"""
        if not self._is_event_valid(event) or not self._check_whitelist(event):
            return

        try:
            recalled_text = await self.memory_manager.recall_relevant_memories(event, event.message_str)
            if recalled_text:
                prefix = f"【记忆上下文】\n{recalled_text}\n\n"
                if hasattr(event.message_obj, 'message') and event.message_obj.message:
                    if isinstance(event.message_obj.message[0], Plain):
                        if "【记忆上下文】" not in event.message_obj.message[0].text:
                            event.message_obj.message[0].text = prefix + event.message_obj.message[0].text
                    else:
                        event.message_obj.message.insert(0, Plain(text=prefix))
                    if hasattr(event, 'message_str'):
                        event.message_str = ''.join(p.text for p in event.message_obj.message if isinstance(p, Plain))
        except Exception as e:
            logger.error(f"记忆召回钩子异常: {e}", exc_info=True)

    # ========== 指令集：/inmem（管理员专用） ==========
    @filter.command_group("inmem", alias={"记忆", "mem"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def inmem_group(self, event: AstrMessageEvent):
        pass

    @inmem_group.command("status")
    async def inmem_status(self, event: AstrMessageEvent):
        try:
            db_path = self.memory_manager._get_db_path(event)
            if not os.path.exists(db_path):
                yield event.plain_result("📊 数据库尚未创建（尚无对话触发总结）")
                return

            with sqlite3.connect(db_path) as conn:
                cursor = conn.cursor()
                cursor.execute("SELECT COUNT(*) FROM memories")
                memory_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM user_profiles")
                profile_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM source_summaries")
                summary_count = cursor.fetchone()[0]
                cursor.execute("SELECT COUNT(*) FROM connections")
                conn_count = cursor.fetchone()[0]

            max_token = self.config.get("max_token_count", 30000)
            keep_last = self.config.get("keep_last_rounds", 10)
            group_iso = self.config.get("group_isolation", True)
            use_emb = self.config.get("use_embedding", True)

            msg = (
                "🧠 无限记忆插件 v1.1.0 状态\n"
                f"📊 数据统计：\n"
                f"  • 记忆总数：{memory_count}\n"
                f"  • 用户画像：{profile_count}\n"
                f"  • 原始总结：{summary_count}\n"
                f"  • 连接关系：{conn_count}\n"
                f"⚙️ 当前配置：\n"
                f"  • Token 阈值：{max_token}\n"
                f"  • 保留轮数：{keep_last}\n"
                f"  • 群隔离：{'✅ 开启' if group_iso else '❌ 关闭'}\n"
                f"  • Embedding：{'✅ 启用' if use_emb else '❌ 禁用'}\n"
                f"📁 数据库：{os.path.basename(db_path)}"
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"/inmem status 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 状态查询失败: {e}")

    @inmem_group.command("recall")
    async def inmem_recall(self, event: AstrMessageEvent, keyword: str = ""):
        if not keyword.strip():
            yield event.plain_result("❌ 请提供关键词，如：/inmem recall 旅行")
            return
        try:
            recalled = await self.memory_manager.recall_relevant_memories(event, keyword)
            if recalled:
                yield event.plain_result(f"✅ 召回结果:\n{recalled}")
            else:
                yield event.plain_result(f"🔍 未找到与 '{keyword}' 相关的记忆。")
        except Exception as e:
            logger.error(f"/inmem recall 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 召回失败: {e}")

    @inmem_group.command("profile")
    async def inmem_profile_by_name(self, event: AstrMessageEvent, name: str = ""):
        if not name.strip():
            yield event.plain_result("❌ 请提供称呼，如：/inmem profile 小雪")
            return
        try:
            db_path = self.memory_manager._get_db_path(event)
            if not os.path.exists(db_path):
                yield event.plain_result("📭 尚无用户画像数据")
                return

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM user_profiles")
                all_profiles = cursor.fetchall()

            matched = []
            for row in all_profiles:
                display_names = json.loads(row['display_names']) if row['display_names'] else []
                if name in display_names:
                    matched.append(row)

            if not matched:
                yield event.plain_result(f"👤 未找到称呼为 '{name}' 的用户画像。")
                return

            lines = [f"👤 称呼 '{name}' 匹配到 {len(matched)} 个画像："]
            for p in matched:
                ts = time.strftime("%m-%d %H:%M", time.localtime(p['last_updated']))
                display_names = json.loads(p['display_names']) if p['display_names'] else []
                traits = json.loads(p['traits']) if p['traits'] else {}
                lines.append(
                    f"• ID: {p['id']}\n"
                    f"  用户ID: {p['user_id']}\n"
                    f"  昵称: {', '.join(display_names)}\n"
                    f"  好感度: {p['affinity']}/100\n"
                    f"  概要: {p['summary'] or '无'}\n"
                    f"  特征: {', '.join([f'{k}:{v}' for k,v in traits.items()]) or '无'}\n"
                    f"  更新: {ts}\n"
                )
            yield event.plain_result("\n".join(lines))
        except Exception as e:
            logger.error(f"/inmem profile 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败: {e}")

    @inmem_group.command("id")
    async def inmem_profile_by_id(self, event: AstrMessageEvent, user_id: str = ""):
        if not user_id.strip():
            yield event.plain_result("❌ 请提供用户ID，如：/inmem id 123456")
            return
        try:
            db_path = self.memory_manager._get_db_path(event)
            if not os.path.exists(db_path):
                yield event.plain_result("📭 尚无用户画像数据")
                return

            with sqlite3.connect(db_path) as conn:
                conn.row_factory = sqlite3.Row
                cursor = conn.cursor()
                cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
                row = cursor.fetchone()

            if not row:
                yield event.plain_result(f"❌ 未找到用户ID {user_id} 的画像。")
                return

            display_names = json.loads(row['display_names']) if row['display_names'] else []
            traits = json.loads(row['traits']) if row['traits'] else {}
            ts = time.strftime("%m-%d %H:%M", time.localtime(row['last_updated']))

            msg = (
                f"👤 用户画像详情\n"
                f"ID: {row['id']}\n"
                f"用户ID: {row['user_id']}\n"
                f"昵称: {', '.join(display_names)}\n"
                f"好感度: {row['affinity']}/100\n"
                f"概要: {row['summary'] or '无'}\n"
                f"特征: {', '.join([f'{k}:{v}' for k,v in traits.items()]) or '无'}\n"
                f"最后更新: {ts}"
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"/inmem id 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败: {e}")

    @inmem_group.command("summary", alias={"总结"})
    async def inmem_force_summarize(self, event: AstrMessageEvent):
        try:
            conversation = await self._get_conversation(event)
            if not conversation or not conversation.history:
                yield event.plain_result("📭 当前无对话历史，无法总结。")
                return

            summary_text = await self._generate_summary(conversation, event)
            if summary_text:
                source_summary_id = await self.memory_manager.store_source_summary(event, summary_text)
                await self.memory_manager.inject_memory(event, summary_text, source_summary_id)
                await self._apply_summary_with_tail(event, conversation, summary_text, source_summary_id)
                yield event.plain_result("✅ 已立即完成总结并注入记忆。")
            else:
                yield event.plain_result("⚠️ 总结生成失败，请检查模型配置。")
        except Exception as e:
            logger.error(f"/inmem summary 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 强制总结失败: {e}")

    # ========== 原插件逻辑 ==========
    def _is_event_valid(self, event: AstrMessageEvent) -> bool:
        return hasattr(event, 'message_obj') and hasattr(event, 'unified_msg_origin')

    def _check_whitelist(self, event: AstrMessageEvent) -> bool:
        whitelist = self.config.get("whitelist", [])
        if not whitelist:
            return True
        current_id = ""
        if hasattr(event.message_obj, 'group_id') and event.message_obj.group_id:
            current_id = event.message_obj.group_id
        elif hasattr(event.message_obj, 'sender') and hasattr(event.message_obj.sender, 'user_id'):
            current_id = event.message_obj.sender.user_id
        whitelist_str = [str(x) for x in whitelist]
        return str(current_id) in whitelist_str

    async def _get_conversation(self, event: AstrMessageEvent):
        conv_mgr = self.context.conversation_manager
        try:
            uid = event.unified_msg_origin
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)
            return await conv_mgr.get_conversation(uid, curr_cid)
        except Exception as e:
            logger.error(f"获取对话失败: {e}")
            return None

    def _estimate_token_count(self, messages: list) -> int:
        total = 0
        for msg in messages:
            content = str(msg.get("content", ""))
            zh_count = sum(1 for c in content if '\u4e00' <= c <= '\u9fff')
            other_count = len(content) - zh_count
            tokens = int(zh_count * 1.5 + other_count * 1.3) + 5
            total += max(tokens, 1)
        return total

    def _should_summarize(self, conversation) -> bool:
        messages = []
        try:
            messages = json.loads(conversation.history)
        except:
            pass
        token_count = self._estimate_token_count(messages)
        max_token = self.config.get("max_token_count", 30000)
        if token_count >= max_token:
            logger.info(f"[Token模式] 当前估算 token: {token_count} ≥ 阈值 {max_token}，触发总结。")
            return True
        return False

    async def _generate_summary(self, conversation, event: AstrMessageEvent) -> str:
        messages = []
        try:
            messages = json.loads(conversation.history)
        except:
            pass

        current_msg_content = getattr(event, 'message_str', '')
        if not current_msg_content and hasattr(event.message_obj, 'message'):
            current_msg_content = "".join([p.text for p in event.message_obj.message if isinstance(p, Plain)])

        if current_msg_content:
            messages.append({"role": "user", "content": current_msg_content})

        history_text = ""
        for msg in messages:
            role = msg.get("role", "unknown")
            content = msg.get("content", "")
            history_text += f"{role}: {content}\n"

        summary_prompt = (
            "请作为第三方观察者，对以下对话历史进行高度概括的总结。你的总结将被用作AI的长期记忆，帮助AI在后续对话中无缝衔接。\n"
            "要求：\n"
            "1. **字数限制**：控制在 500 字以内，言简意赅。\n"
            "2. **格式要求**：请直接输出总结内容，不要包含任何开场白或结束语。总结内容必须以“【前情提要】”开头。\n"
            "3. **内容重点**：\n"
            "   - 参与者的身份、称呼及关系。\n"
            "   - 已完成的关键任务、达成的共识或重要决策。\n"
            "   - 当前正在进行但未完成的话题或任务。\n"
            "   - 重要的上下文约束（如用户偏好、设定的场景规则等）。\n"
            "   - AI（你）在总结中则表述你自己的身份，这将会给未来的你自己看。\n"
            "   - 并且要让未来的你自己明白这个前情提要并非来自用户所为，而是全自动总结。\n"
            "   - 在结尾添加你最后说了什么，用户最后说了什么。\n"
            "4. **语气**：使用客观、陈述性的语气。\n\n"
            f"对话记录：\n{history_text}"
        )

        target_provider_id = self.config.get("summary_provider_id")
        max_retries = self.config.get("max_retries", 3)
        uid = event.unified_msg_origin

        # ✅ 关键修正：get_current_chat_provider_id 需传 umo=uid
        current_provider_id = None
        try:
            current_provider_id = await self.context.get_current_chat_provider_id(umo=uid)
        except Exception as e:
            logger.error(f"获取当前模型提供商 ID 失败: {e}")

        summary = None
        for i in range(max_retries):
            logger.info(f"正在尝试生成总结 (第 {i+1}/{max_retries} 次)...")
            providers_to_try = []
            if target_provider_id:
                providers_to_try.append(target_provider_id)
                if current_provider_id and current_provider_id != target_provider_id:
                    providers_to_try.append(current_provider_id)
            elif current_provider_id:
                providers_to_try.append(current_provider_id)

            if not providers_to_try:
                logger.error("未找到可用的模型提供商 ID。")
                return None

            for pid in providers_to_try:
                try:
                    logger.info(f"正在使用提供商 {pid} 生成总结...")
                    llm_resp = await self.context.llm_generate(
                        chat_provider_id=pid,
                        prompt=summary_prompt,
                        contexts=[]
                    )
                    if llm_resp and hasattr(llm_resp, 'completion_text') and llm_resp.completion_text:
                        summary = llm_resp.completion_text
                        logger.info(f"✅ 总结生成成功: {summary[:60]}...")
                        return summary
                except Exception as e:
                    logger.warning(f"使用提供商 {pid} 生成总结失败: {e}")

        logger.error("⚠️ 所有重试均失败。放弃本次总结。")
        try:
            # ✅ v4.x send_message 接收单个组件
            await self.context.send_message(
                event.unified_msg_origin,
                Plain("【无限记忆插件警告】\n总结系统故障，无法连接到模型提供商。\n本次总结已放弃，对话历史将保留。请检查模型配置或网络连接。")
            )
        except Exception as e:
            logger.error(f"发送警告消息失败: {e}")
        return None

    async def _apply_summary_with_tail(self, event: AstrMessageEvent, conversation, summary: str, source_summary_id: int = None):
        conv_mgr = self.context.conversation_manager
        uid = event.unified_msg_origin
        curr_cid = getattr(conversation, "cid", None)
        if not curr_cid:
            curr_cid = await conv_mgr.get_curr_conversation_id(uid)

        try:
            raw_history = []
            try:
                raw_history = json.loads(conversation.history)
            except Exception as e:
                logger.warning(f"解析历史失败: {e}")

            keep_last = self.config.get("keep_last_rounds", 10)
            tail_messages = raw_history[-keep_last:] if len(raw_history) > keep_last else raw_history[:]

            if hasattr(conv_mgr, "delete_conversation"):
                await conv_mgr.delete_conversation(uid, curr_cid)

            new_conv = None
            if hasattr(conv_mgr, "new_conversation"):
                new_conv_or_cid = await conv_mgr.new_conversation(uid)
                if isinstance(new_conv_or_cid, str):
                    cid = new_conv_or_cid
                    await asyncio.sleep(0.1)
                    new_conv = await conv_mgr.get_conversation(uid, cid)
                else:
                    new_conv = new_conv_or_cid
            if not new_conv:
                raise RuntimeError("无法创建新对话")

            new_history = [
                {"role": "assistant", "content": f"【前情提要】\n{summary}"}
            ] + tail_messages

            new_conv.history = json.dumps(new_history, ensure_ascii=False)

            saved = False
            if hasattr(conv_mgr, "save_conversation"):
                try:
                    await conv_mgr.save_conversation(new_conv)
                    saved = True
                except Exception as e:
                    logger.warning(f"save_conversation 失败: {e}")

            if not saved and hasattr(conv_mgr, "update_conversation"):
                try:
                    await conv_mgr.update_conversation(new_conv)
                except TypeError as e:
                    if "unhashable type" in str(e):
                        logger.warning(f"update_conversation 抛出 unhashable type 错误，忽略: {e}")

            try:
                summary_prefix = f"【前情提要】\n{summary}\n"
                if hasattr(event.message_obj, 'message') and event.message_obj.message:
                    first_is_text = isinstance(event.message_obj.message[0], Plain)
                    if first_is_text:
                        if "【前情提要】" not in event.message_obj.message[0].text:
                            event.message_obj.message[0].text = summary_prefix + event.message_obj.message[0].text
                    else:
                        event.message_obj.message.insert(0, Plain(text=summary_prefix))
                if hasattr(event, "message_str"):
                    try:
                        event.message_str = "".join([p.text for p in event.message_obj.message if isinstance(p, Plain)])
                    except:
                        pass
            except Exception as e:
                logger.error(f"注入摘要到消息对象失败: {e}")

            logger.info(f"✅ 新对话已创建：1 条摘要 + {len(tail_messages)} 条最新消息（保留最后 {keep_last} 轮）。")

        except Exception as e:
            logger.error(f"应用摘要+尾部保留时出错: {e}", exc_info=True)
            raise

    async def terminate(self):
        pass