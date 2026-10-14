-- 0002_channel_sessions.sql —— 入口会话路由（规格书 3.8）
-- (channel, user_id) -> session_id 的持久映射：
--   scope=user    同一用户跨入口共用一条会话（scope_key = user:<user_id>）
--   scope=channel 按 (渠道, 用户) 各一条（scope_key = <channel>:<user_id>）
-- 只存路由，不存对话内容；对话仍在 messages 表。

CREATE TABLE IF NOT EXISTS channel_sessions (
  scope_key  TEXT PRIMARY KEY,
  channel    TEXT,
  user_id    TEXT,
  session_id TEXT NOT NULL,
  created_at INTEGER,
  updated_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_channel_sessions_session ON channel_sessions(session_id);
