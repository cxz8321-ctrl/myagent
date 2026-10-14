-- 0003：消息软删除
-- 铁律是"只插不改、一切软删除"，但 messages 建表时漏了 status 列。
-- 补上它，这样误发的消息可以"撤回"（改 status），而不是物理删。
ALTER TABLE messages ADD COLUMN status TEXT NOT NULL DEFAULT 'active';
CREATE INDEX IF NOT EXISTS idx_messages_status ON messages(session_id, status, id);
