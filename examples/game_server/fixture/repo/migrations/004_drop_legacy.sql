-- 004_drop_legacy.sql
-- 故意留成：破坏性变更、没有备份、没有事务（审计会报 migration_destructive / migration_no_tx）
ALTER TABLE player DROP COLUMN legacy_score;