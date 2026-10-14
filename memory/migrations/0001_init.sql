-- 0001_init.sql —— 记忆系统建表（规格书附录 B）

CREATE TABLE IF NOT EXISTS sessions (
  id                 TEXT PRIMARY KEY,
  user_id            TEXT,
  channel            TEXT,
  title              TEXT,
  created_at         INTEGER,
  updated_at         INTEGER,
  last_message_at    INTEGER,
  message_count      INTEGER DEFAULT 0,
  summary            TEXT,
  summary_upto_msg_id INTEGER,
  status             TEXT DEFAULT 'active',
  meta               TEXT
);
CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, updated_at DESC);
CREATE INDEX IF NOT EXISTS idx_sessions_channel ON sessions(channel, status);

-- L1 情景记忆：append-only，只插不改
CREATE TABLE IF NOT EXISTS messages (
  id            INTEGER PRIMARY KEY AUTOINCREMENT,
  session_id    TEXT NOT NULL,
  role          TEXT NOT NULL,
  content       TEXT,
  tool_calls    TEXT,
  tool_call_id  TEXT,
  name          TEXT,
  token_count   INTEGER,
  created_at    INTEGER,
  trace_id      TEXT,
  client_msg_id TEXT,
  FOREIGN KEY(session_id) REFERENCES sessions(id)
);
CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, id);
CREATE INDEX IF NOT EXISTS idx_messages_created ON messages(created_at);
CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_clientid ON messages(session_id, client_msg_id)
  WHERE client_msg_id IS NOT NULL;

-- 大内容旁路：>8KB 走这里，messages.content 只存 blob:<id>
CREATE TABLE IF NOT EXISTS blobs (
  id         TEXT PRIMARY KEY,
  sha256     TEXT UNIQUE,
  content    TEXT,
  size       INTEGER,
  created_at INTEGER
);

-- L2 语义记忆
CREATE TABLE IF NOT EXISTS memories (
  id                TEXT PRIMARY KEY,
  user_id           TEXT,
  scope             TEXT,
  type              TEXT,
  content           TEXT,
  content_hash      TEXT,
  importance        REAL DEFAULT 0.5,
  confidence        REAL DEFAULT 1.0,
  source            TEXT,
  source_session_id TEXT,
  source_message_id INTEGER,
  created_at        INTEGER,
  updated_at        INTEGER,
  last_accessed_at  INTEGER,
  access_count      INTEGER DEFAULT 0,
  expires_at        INTEGER,
  status            TEXT DEFAULT 'active',
  superseded_by     TEXT,
  tags              TEXT
);
CREATE INDEX IF NOT EXISTS idx_memories_scope ON memories(user_id, scope, type, status);
CREATE INDEX IF NOT EXISTS idx_memories_hash ON memories(content_hash);
CREATE INDEX IF NOT EXISTS idx_memories_access ON memories(last_accessed_at);

-- 向量与主表分离：换 embedding 模型只重建这张
CREATE TABLE IF NOT EXISTS memory_vectors (
  memory_id   TEXT NOT NULL,
  chunk_index INTEGER DEFAULT 0,
  embedding   BLOB,
  model       TEXT,
  dim         INTEGER,
  created_at  INTEGER,
  PRIMARY KEY (memory_id, chunk_index),
  FOREIGN KEY(memory_id) REFERENCES memories(id)
);
CREATE INDEX IF NOT EXISTS idx_vec_memory ON memory_vectors(memory_id);

CREATE TABLE IF NOT EXISTS memory_links (
  from_id  TEXT,
  to_id    TEXT,
  relation TEXT,
  weight   REAL,
  PRIMARY KEY (from_id, to_id, relation)
);

-- L3 档案层：键值，不走向量，开场全量注入
CREATE TABLE IF NOT EXISTS profile (
  user_id    TEXT,
  key        TEXT,
  value      TEXT,
  value_type TEXT DEFAULT 'string',
  confidence REAL DEFAULT 1.0,
  source     TEXT,
  updated_at INTEGER,
  PRIMARY KEY (user_id, key)
);

-- L4 程序性记忆：文件 + 索引（文件在 memory/procedural/*.md）
CREATE TABLE IF NOT EXISTS procedural_index (
  path       TEXT PRIMARY KEY,
  title      TEXT,
  summary    TEXT,
  updated_at INTEGER,
  mtime_hash TEXT
);

-- 审计：工具调用与记忆写入共用一张
CREATE TABLE IF NOT EXISTS audit_log (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  ts          INTEGER,
  actor       TEXT,
  target_type TEXT,
  action      TEXT,
  target_id   TEXT,
  detail      TEXT,
  approved    INTEGER
);
CREATE INDEX IF NOT EXISTS idx_audit_target ON audit_log(target_type, target_id, ts);
