from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register, StarTools
from astrbot.api import logger, AstrBotConfig
from astrbot.api.provider import LLMResponse
from astrbot.core.message.components import Plain
import json
import asyncio
import os
import time
from .memory_manager import MemoryManager


@register(
    "astrbot_plugin_infinite_memory",
    "ThriEy",
    "无限记忆插件：自动总结对话历史，构建记忆图谱",
    "v1.1.2",
    "https://github.com/ThriEy/astrbot_plugin_infinite_memory"
)
class InfiniteMemoryPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config
        self.data_dir = StarTools.get_data_dir()
        os.makedirs(self.data_dir, exist_ok=True)
        self.memory_manager = MemoryManager(context, config, self.data_dir)
        
        # 状态管理（防抖 + 防重入）
        self.session_trigger_count = {}   # {session_id: 连续超阈值次数}
        self.session_summarizing = set()  # {session_id}
        self.session_token_count = {}     # {session_id: 累计真实 token}
        
        logger.info("🧠 无限记忆插件 v1.1.2 启动")

    # ========== 真实 Token 统计钩子 ==========
    @filter.on_llm_response()
    async def _on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """仅统计 bot 实际处理的请求"""
        try:
            session_id = event.unified_msg_origin
            usage = getattr(resp.raw_completion, "usage", None)
            if not usage or not hasattr(usage, "total_tokens"):
                return
            real_tokens = usage.total_tokens
            self.session_token_count[session_id] = self.session_token_count.get(session_id, 0) + real_tokens
            logger.debug(f"[Token统计] 会话 {session_id} 累计: {self.session_token_count[session_id]}")
        except Exception as e:
            logger.warning(f"Token 统计失败: {e}")

    # ========== 核心：对话总结 ==========
    @filter.event_message_type(filter.EventMessageType.ALL, priority=100)
    async def on_message(self, event: AstrMessageEvent, *args, **kwargs):
        if not self._is_event_valid(event) or not self._check_whitelist(event):
            return

        conversation = await self._get_conversation(event)
        if not conversation or not conversation.history:
            return

        try:
            session_id = event.unified_msg_origin
            if session_id in self.session_summarizing:
                logger.debug(f"[防重入] 会话 {session_id} 总结进行中，跳过")
                return

            current_tokens = self.session_token_count.get(session_id, 0)
            max_token = self.config.get("max_token_count", 10000)
            
            if current_tokens >= max_token:
                #防抖：连续 3 次超阈值才触发
                self.session_trigger_count[session_id] = self.session_trigger_count.get(session_id, 0) + 1
                logger.debug(f"[防抖动] 会话 {session_id} 连续超阈值: {self.session_trigger_count[session_id]}/3")
                
                if self.session_trigger_count[session_id] >= 3:
                    self.session_trigger_count[session_id] = 0
                    logger.info(f"[真实Token] 会话 {session_id} 累计 {current_tokens} ≥ {max_token}，连续3次超阈值，触发总结")
                    
                    self.session_summarizing.add(session_id)
                    try:
                        summary_text = await self._generate_summary(conversation, event)
                        if summary_text:
                            #最小轮次保护：至少 5 轮
                            raw_history = []
                            try:
                                raw_history = json.loads(conversation.history)
                            except:
                                pass
                            if len(raw_history) < 5:
                                logger.warning(f"[保护] 会话历史仅 {len(raw_history)} 轮 < 5，跳过清理")
                                self.session_token_count[session_id] = 0
                                return
                            
                            self.session_token_count[session_id] = 0
                            source_summary_id = await self.memory_manager.store_source_summary(event, summary_text)
                            await self.memory_manager.inject_memory(event, summary_text, source_summary_id)
                            await self._apply_summary_with_tail(event, conversation, summary_text, source_summary_id)
                    finally:
                        self.session_summarizing.discard(session_id)
            else:
                #仅当明显回落（<80%）才清零计数器
                if current_tokens < max_token * 0.8:
                    self.session_trigger_count.pop(session_id, None)
                    
        except Exception as e:
            logger.error(f"处理消息时发生错误: {e}", exc_info=True)
            session_id = getattr(event, 'unified_msg_origin', 'unknown')
            self.session_summarizing.discard(session_id)

    # ========== 记忆召回钩子 ==========
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

    # ========== 指令集：/inmem ==========
    @filter.command_group("inmem", alias={"记忆", "mem"})
    @filter.permission_type(filter.PermissionType.ADMIN)
    async def inmem_group(self, event: AstrMessageEvent):
        pass

    @inmem_group.command("status")
    async def inmem_status(self, event: AstrMessageEvent):
        """查看插件状态"""
        try:
            db_path = self.memory_manager._get_db_path(event)
            if not os.path.exists(db_path):
                yield event.plain_result("📊 数据库尚未创建")
                return

            #修正：封装调用 MemoryManager
            stats = self.memory_manager.get_memory_statistics(db_path)
            max_token = self.config.get("max_token_count", 10000)
            keep_last = self.config.get("keep_last_rounds", 10)
            group_iso = self.config.get("group_isolation", True)
            use_emb = self.config.get("use_embedding", True)
            emb_provider = self.config.get("embedding_provider_id", "默认")

            msg = (
                "🧠 无限记忆插件状态\n"
                f"📊 数据统计：\n"
                f"  • 记忆总数：{stats['memory_count']}\n"
                f"  • 用户画像：{stats['profile_count']}\n"
                f"  • 原始总结：{stats['summary_count']}\n"
                f"  • 连接关系：{stats['conn_count']}\n"
                f"⚙️ 当前配置：\n"
                f"  • Token 阈值：{max_token}\n"
                f"  • 保留轮数：{keep_last}\n"
                f"  • 群隔离：{'✅ 开启' if group_iso else '❌ 关闭'}\n"
                f"  • Embedding：{'✅ 启用' if use_emb else '❌ 禁用'}\n"
                f"  • Embedding 模型：{emb_provider}\n"
                f"📁 数据库：{os.path.basename(db_path)}\n"
                f"🔢 当前会话累计 token：{self.session_token_count.get(event.unified_msg_origin, 0)}"
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"/inmem status 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 状态查询失败: {e}")

    @inmem_group.command("recall")
    async def inmem_recall(self, event: AstrMessageEvent, keyword: str = ""):
        if not keyword.strip():
            yield event.plain_result("❌ 请提供关键词")
            return
        try:
            recalled = await self.memory_manager.recall_relevant_memories(event, keyword)
            if recalled:
                yield event.plain_result(f"✅ 召回结果:\n{recalled}")
            else:
                yield event.plain_result(f"🔍 未找到相关记忆")
        except Exception as e:
            logger.error(f"/inmem recall 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 召回失败: {e}")

    @inmem_group.command("profile")
    async def inmem_profile_by_name(self, event: AstrMessageEvent, name: str = ""):
        if not name.strip():
            yield event.plain_result("❌ 请提供称呼")
            return
        try:
            db_path = self.memory_manager._get_db_path(event)
            if not os.path.exists(db_path):
                yield event.plain_result("📭 尚无用户画像数据")
                return

            #修正：封装调用
            profiles = self.memory_manager.search_profiles_by_name(db_path, name)
            if not profiles:
                yield event.plain_result(f"👤 未找到称呼为 '{name}' 的用户画像")
                return

            lines = [f"👤 称呼 '{name}' 匹配到 {len(profiles)} 个画像："]
            for p in profiles:
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
            yield event.plain_result("❌ 请提供用户ID")
            return
        try:
            db_path = self.memory_manager._get_db_path(event)
            if not os.path.exists(db_path):
                yield event.plain_result("📭 尚无用户画像数据")
                return

            #修正：封装调用
            profile = self.memory_manager.get_profile_by_user_id(db_path, user_id)
            if not profile:
                yield event.plain_result(f"❌ 未找到用户ID {user_id} 的画像")
                return

            display_names = json.loads(profile['display_names']) if profile['display_names'] else []
            traits = json.loads(profile['traits']) if profile['traits'] else {}
            ts = time.strftime("%m-%d %H:%M", time.localtime(profile['last_updated']))

            msg = (
                f"👤 用户画像详情\n"
                f"ID: {profile['id']}\n"
                f"用户ID: {profile['user_id']}\n"
                f"昵称: {', '.join(display_names)}\n"
                f"好感度: {profile['affinity']}/100\n"
                f"概要: {profile['summary'] or '无'}\n"
                f"特征: {', '.join([f'{k}:{v}' for k,v in traits.items()]) or '无'}\n"
                f"最后更新: {ts}"
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"/inmem id 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败: {e}")

    @inmem_group.command("token")
    async def inmem_token_usage(self, event: AstrMessageEvent):
        """查看当前会话累计 token"""
        try:
            session_id = event.unified_msg_origin
            current_tokens = self.session_token_count.get(session_id, 0)
            max_token = self.config.get("max_token_count", 10000)
            progress = min(100, int(current_tokens / max_token * 100)) if max_token > 0 else 0
            
            bar_len = 10
            filled = "█" * int(bar_len * progress / 100)
            empty = "░" * (bar_len - len(filled))
            progress_bar = f"[{filled}{empty}] {progress}%"
            
            msg = (
                f"📊 Token 使用统计\n"
                f"━━━━━━━━━━━━━━\n"
                f"• 累计消耗: {current_tokens}\n"
                f"• 触发阈值: {max_token}\n"
                f"• 使用进度: {progress_bar}\n"
                f"━━━━━━━━━━━━━━\n"
                f"💡 说明：仅统计 bot 实际处理的请求"
            )
            yield event.plain_result(msg)
        except Exception as e:
            logger.error(f"/inmem token 错误: {e}", exc_info=True)
            yield event.plain_result(f"❌ 查询失败: {e}")

    @inmem_group.command("summary", alias={"总结"})
    async def inmem_force_summarize(self, event: AstrMessageEvent):
        """立即触发总结"""
        try:
            conversation = await self._get_conversation(event)
            if not conversation or not conversation.history:
                yield event.plain_result("📭 当前无对话历史")
                return

            session_id = event.unified_msg_origin
            if session_id in self.session_summarizing:
                yield event.plain_result("⏳ 总结进行中，请稍后再试")
                return

            self.session_summarizing.add(session_id)
            try:
                summary_text = await self._generate_summary(conversation, event)
                if summary_text:
                    raw_history = []
                    try:
                        raw_history = json.loads(conversation.history)
                    except:
                        pass
                    if len(raw_history) < 5:
                        yield event.plain_result(f"⚠️ 会话历史仅 {len(raw_history)} 轮 < 5，跳过总结")
                        return

                    self.session_token_count[session_id] = 0
                    source_summary_id = await self.memory_manager.store_source_summary(event, summary_text)
                    await self.memory_manager.inject_memory(event, summary_text, source_summary_id)
                    await self._apply_summary_with_tail(event, conversation, summary_text, source_summary_id)
                    yield event.plain_result("✅ 已立即完成总结并注入记忆")
                else:
                    yield event.plain_result("⚠️ 总结生成失败")
            finally:
                self.session_summarizing.discard(session_id)
        except Exception as e:
            logger.error(f"/inmem summary 错误: {e}", exc_info=True)
            self.session_summarizing.discard(event.unified_msg_origin)
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
            "你是一个专业的对话分析师，需要将群聊/私聊历史总结为结构化记忆。\n"
            "要求：\n"
            "1. **字数限制**：≤500字，言简意赅\n"
            "2. **格式要求**：\n"
            "   - 以「【前情提要】」开头\n"
            "   - 直接输出内容，无开场白/结束语\n"
            "3. **内容重点**：\n"
            "   - 【参与者列表】（必须包含）：按以下格式逐行列出：\n"
            "       • 用户名（ID: 数字ID，角色：身份，相关度：【核心/活跃/提及】）\n"
            "       • 你（AI角色名，相关度：【核心】）\n"
            "   - 关键任务/决策：已完成的重要事项\n"
            "   - 进行中事项：尚未完成的话题\n"
            "   - 用户特征：显著的性格/偏好\n"
            "4. **相关度定义**：\n"
            "   - 【核心】：发起话题、做决策、多次主导对话\n"
            "   - 【活跃】：≥2轮有效发言、提供关键信息\n"
            "   - 【提及】：被他人提到但未直接参与\n"
            "5. **ID 推断规则**：\n"
            "   - 用户ID = 消息中的 sender.user_id（如 2980223165）\n"
            "   - 若未明确，用已知ID或'unknown'\n"
            "6. **语气**：客观、陈述式\n\n"
            f"对话记录：\n{history_text}"
        )

        target_provider_id = self.config.get("summary_provider_id")
        max_retries = self.config.get("max_retries", 3)
        uid = event.unified_msg_origin

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
                        event.message_str = "".join([
                            p.text for p in event.message_obj.message 
                            if isinstance(p, Plain)
                        ])
                    except:
                        pass
            except Exception as e:
                logger.error(f"注入摘要到消息对象失败: {e}")

            logger.info(f"✅ 新对话已创建：1 条摘要 + {len(tail_messages)} 条最新消息")

        except Exception as e:
            logger.error(f"应用摘要+尾部保留时出错: {e}", exc_info=True)
            raise

    async def terminate(self):
        pass