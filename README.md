# 无限记忆插件
> **根据token自动总结上下文，同时采用记忆图存储记忆，让AI拥有长期无限记忆**

## 🌟 核心功能

- **无限对话**：自动总结对话历史，突破LLM上下文窗口限制
- **多用户画像**：为每个参与者构建个性化画像，记录偏好与关系
- **精准记忆召回**：关键词+语义搜索，智能提取相关记忆
- **群聊隔离**：为每个群组创建独立记忆数据库，保障数据安全
- **故障保护**：多重降级策略，确保系统稳健运行
- **Token优化**：基于真实Token统计，避免非bot对话触发总结

## 📦 安装指南

### 方法一：Git克隆（推荐）

```bash
# 进入AstrBot插件目录
cd /path/to/AstrBot/data/plugins/

# 克隆插件仓库
git clone https://github.com/ThriEy/astrbot_plugin_infinite_memory.git

# 重启AstrBot
systemctl restart astrbot  # 或使用你的启动命令
```

### 方法二：手动安装

1. 下载[最新release](https://github.com/ThriEy/astrbot_plugin_infinite_memory/releases)
2. 解压后将`astrbot_plugin_infinite_memory`文件夹放入`AstrBot/data/plugins/`目录
3. 重启AstrBot服务

## ⚙️ 配置说明

在AstrBot WebUI的「插件管理」中启用插件后，可配置以下参数：

| 参数名 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `max_token_count` | 整数 | 10000 | 触发总结的token阈值（建议设为模型上下文的70-80%） |
| `keep_last_rounds` | 整数 | 10 | 总结时保留的最新对话轮数，确保上下文连贯 |
| `group_isolation` | 布尔 | true | 为每个群组创建独立记忆数据库，保障数据隔离 |
| `use_embedding` | 布尔 | true | 启用embedding模型提升记忆召回精准度 |
| `summary_provider_id` | 字符串 | (空) | 用于总结的模型提供商ID（如openai, gemini），留空使用当前会话模型 |
| `embedding_provider_id` | 字符串 | (空) | 用于生成记忆向量的Embedding Provider ID，留空使用默认配置 |
| `max_retries` | 整数 | 3 | 总结失败时的最大重试次数 |
| `whitelist` | 列表 | [] | 白名单列表（群号或QQ号），留空则允许所有会话 |

## 📋 管理员指令集

> 所有`/inmem`指令仅限管理员使用

| 指令 | 功能 | 示例 |
|------|------|------|
| `/inmem` | 显示指令帮助 | `/inmem` |
| `/inmem status` | 查看插件状态与统计数据 | `/inmem status` |
| `/inmem token` | 查看当前会话累计token | `/inmem token` |
| `/inmem recall <关键词>` | 测试记忆召回功能 | `/inmem recall 旅行` |
| `/inmem profile <称呼>` | 按称呼查询用户画像 | `/inmem profile 小雪` |
| `/inmem id <用户ID>` | 按用户ID查询画像 | `/inmem id 123456` |
| `/inmem summary` | 立即触发总结（无视阈值） | `/inmem summary` |

### 指令示例

```
/inmem status
🧠 无限记忆插件 v1.1.1 状态
📊 数据统计：
  • 记忆总数：24
  • 用户画像：3
  • 原始总结：24
  • 连接关系：48
⚙️ 当前配置：
  • Token 阈值：10000
  • 保留轮数：10
  • 群隔离：✅ 开启
  • Embedding：✅ 启用
  • Embedding 模型：默认
📁 数据库：memories_2167039796.db
🔢 当前会话累计 token：8742
```

## 🧠 工作原理

1. **Token监控**：仅统计bot实际处理的请求，非bot对话不计入
2. **自动总结**：当累计token达到阈值，调用LLM生成对话总结
3. **记忆构建**：
   - 将总结压缩为≤150字的精简记忆
   - 识别高相关度用户（【核心】/【活跃】），更新用户画像
   - 为记忆与用户创建双向连接关系
4. **上下文重建**：
   - 创建新对话，注入【前情提要】
   - 保留最后N轮对话，确保无缝衔接
5. **智能召回**：
   - 对话中识别关键词/关键信息
   - 从记忆图谱中检索相关记忆
   - LLM压缩整合为≤200字上下文，自动注入对话

## ❓ 常见问题

### Q: 为什么设置了10000 token阈值，但对话很少就触发总结？
A: 早期版本基于消息轮次估算token，不准确。当前版本使用**真实token统计**，仅计算bot实际处理的请求，非bot对话（纯用户间聊天）不计入，彻底解决此问题。

### Q: 怎么查看当前会话用了多少token？
A: 管理员可使用`/inmem token`命令查看当前会话累计token使用量和进度条。

### Q: 记忆召回不准确怎么办？
A: 建议：
1. 启用`use_embedding`选项并配置合适的embedding模型
2. 增加`max_token_count`值，减少总结频率，保留更多上下文
3. 通过`/inmem summary`手动触发总结，让AI重新学习上下文

### Q: 如何清理过期记忆？
A: 插件会自动清理超过30天未使用的低权重记忆。也可通过调整配置降低总结频率，或定期使用`/inmem status`查看数据量，必要时重置会话。

### Q: 总结失败怎么办？
A: 系统会：
1. 优先使用`summary_provider_id`指定的模型
2. 失败则降级到当前会话模型
3. 所有尝试失败后，向管理员发送警告，保留当前对话历史
4. 等待下次触发时再次尝试总结

## 🛡️ 故障保护机制

- **多重降级**：总结失败时自动尝试备选模型
- **数据安全**：所有操作前先备份，失败自动回滚
- **内存保护**：自动清理低权重、长期未使用的记忆
- **资源监控**：实时跟踪token使用，防止超限
- **优雅降级**：当embedding服务不可用时，自动回退到关键词匹配

## 🚀 未来更新计划

- **记忆整理**：自动触发的记忆整理系统
- **记忆遗忘系统**：重新设计记忆遗忘衰减机制
-  **WEBUI**：便于修改记忆的可视化界面

## 🚀 版本信息

- **插件版本**：v1.1.1
- **兼容AstrBot**：≥ 4.0.0
- **最后更新**：2025-11-29

## 🙏 鸣谢

参考插件:
- [Alan Backer](https://github.com/AlanBacker) - 无限对话功能原型
- [FengYing](https://github.com/) - Token统计机制参考

## 📜 许可证

本项目采用 [MIT 许可证](LICENSE)。

---

⭐ **觉得这个插件有用？** 请在 GitHub 上给我们一颗星！  
🐞 **遇到问题？** 请在 [Issues](https://github.com/ThriEy/astrbot_plugin_infinite_memory/issues) 提交反馈。  
🤝 **想要贡献？** 欢迎提交 PR！


