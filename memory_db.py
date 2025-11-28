import sqlite3
import os
import json
import time
from typing import Optional, List, Dict, Any
from astrbot.api import logger


class MemoryDB:
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    def _init_db(self):
        """初始化数据库结构"""
        os.makedirs(os.path.dirname(self.db_path), exist_ok=True)
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 表1: 原始总结存储
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS source_summaries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                content TEXT NOT NULL,
                timestamp INTEGER NOT NULL,
                token_count INTEGER
            )
            ''')
            # 表2: 精简记忆
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS memories (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                summary_id INTEGER NOT NULL,
                summary TEXT NOT NULL,
                keywords TEXT,
                timestamp INTEGER NOT NULL,
                weight REAL DEFAULT 1.0,
                embedding BLOB,
                FOREIGN KEY (summary_id) REFERENCES source_summaries(id)
            )
            ''')
            # 表3: 用户画像（affinity 范围 -100~100）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS user_profiles (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                user_id TEXT NOT NULL UNIQUE,
                display_names TEXT,
                summary TEXT,
                affinity INTEGER DEFAULT 0,  -- 明确为 INTEGER
                traits TEXT,
                last_updated INTEGER,
                embedding BLOB
            )
            ''')
            # 表4: 连接关系（双单向连接）
            cursor.execute('''
            CREATE TABLE IF NOT EXISTS connections (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                from_id INTEGER NOT NULL,
                to_id INTEGER NOT NULL,
                type TEXT NOT NULL,
                strength REAL DEFAULT 1.0,
                direction TEXT NOT NULL CHECK(direction IN ('forward', 'backward')),
                keywords TEXT,
                created_at INTEGER NOT NULL
            )
            ''')
            # 创建索引
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_memories_timestamp ON memories(timestamp)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_connections_from_to ON connections(from_id, to_id)')
            cursor.execute('CREATE INDEX IF NOT EXISTS idx_user_profiles_user_id ON user_profiles(user_id)')
            conn.commit()

    # ===== 原始总结操作 =====
    def save_source_summary(self, content: str, token_count: int = None) -> int:
        """保存原始总结，返回ID"""
        timestamp = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                "INSERT INTO source_summaries (content, timestamp, token_count) VALUES (?, ?, ?)",
                (content, timestamp, token_count)
            )
            conn.commit()
            return cursor.lastrowid

    def get_source_summary(self, summary_id: int) -> Optional[Dict[str, Any]]:
        """获取原始总结"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            cursor.execute("SELECT * FROM source_summaries WHERE id = ?", (summary_id,))
            row = cursor.fetchone()
            return dict(row) if row else None

    # ===== 记忆操作 =====
    def save_memory(self, summary_id: int, summary: str, keywords: List[str] = None, 
                   weight: float = 1.0, embedding: bytes = None) -> int:
        """保存精简记忆"""
        timestamp = int(time.time())
        keywords_json = json.dumps(keywords) if keywords else None
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO memories 
                (summary_id, summary, keywords, timestamp, weight, embedding) 
                VALUES (?, ?, ?, ?, ?, ?)""",
                (summary_id, summary, keywords_json, timestamp, weight, embedding)
            )
            conn.commit()
            return cursor.lastrowid

    # ===== 用户画像操作 =====
    def get_or_create_user_profile(self, user_id: str, display_name: str = None) -> Dict[str, Any]:
        """获取或创建用户画像"""
        with sqlite3.connect(self.db_path) as conn:
            conn.row_factory = sqlite3.Row
            cursor = conn.cursor()
            # 尝试获取现有画像
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if row:
                profile = dict(row)
                # 更新显示名称
                if display_name:
                    display_names = json.loads(profile.get('display_names', '[]'))
                    if display_name not in display_names:
                        display_names.append(display_name)
                        cursor.execute(
                            "UPDATE user_profiles SET display_names = ?, last_updated = ? WHERE user_id = ?",
                            (json.dumps(display_names), int(time.time()), user_id)
                        )
                        conn.commit()
                return profile
            # 创建新画像（✅ affinity 明确为 0）
            display_names = [display_name] if display_name else []
            cursor.execute(
                """INSERT INTO user_profiles 
                (user_id, display_names, affinity, last_updated) 
                VALUES (?, ?, ?, ?)""",
                (user_id, json.dumps(display_names), 0, int(time.time()))  # ← int 0
            )
            conn.commit()
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            new_row = cursor.fetchone()
            return dict(new_row) if new_row else None

    def update_user_profile(self, user_id: str, summary: str = None, affinity_change: int = 0,
                          traits: Dict[str, Any] = None, embedding: bytes = None):
        """更新用户画像（✅ 修复 str/int 类型错误）"""
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            # 获取当前画像
            cursor.execute("SELECT * FROM user_profiles WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            if not row:
                return

            # ✅ 关键修正：三重保障处理 affinity 类型
            current_affinity = row[5]  # affinity 字段（索引5）
            if current_affinity is None:
                current_affinity = 0
            elif isinstance(current_affinity, str):
                try:
                    current_affinity = int(current_affinity)
                except (ValueError, TypeError):
                    current_affinity = 0
            # 此时 current_affinity 一定是 int
            
            new_affinity = max(-100, min(100, current_affinity + affinity_change))
            
            update_fields = []
            params = []
            if summary is not None:
                update_fields.append("summary = ?")
                params.append(summary)
            update_fields.append("affinity = ?")
            params.append(new_affinity)
            if traits is not None:
                update_fields.append("traits = ?")
                params.append(json.dumps(traits))
            if embedding is not None:
                update_fields.append("embedding = ?")
                params.append(embedding)
            update_fields.append("last_updated = ?")
            params.append(int(time.time()))
            params.append(user_id)
            
            query = f"UPDATE user_profiles SET {', '.join(update_fields)} WHERE user_id = ?"
            cursor.execute(query, params)
            conn.commit()

    # ===== 连接操作 =====
    def create_connection(self, from_id: int, to_id: int, conn_type: str, 
                         strength: float = 1.0, direction: str = "forward", 
                         keywords: List[str] = None):
        """创建连接（单向）"""
        if direction not in ["forward", "backward"]:
            raise ValueError("direction 必须是 'forward' 或 'backward'")
        keywords_json = json.dumps(keywords) if keywords else None
        created_at = int(time.time())
        with sqlite3.connect(self.db_path) as conn:
            cursor = conn.cursor()
            cursor.execute(
                """INSERT INTO connections 
                (from_id, to_id, type, strength, direction, keywords, created_at) 
                VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (from_id, to_id, conn_type, strength, direction, keywords_json, created_at)
            )
            conn.commit()
            return cursor.lastrowid

    # ===== 辅助方法 =====
    def close(self):
        """关闭连接（上下文管理器自动处理）"""
        pass

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc_val, exc_tb):
        self.close()