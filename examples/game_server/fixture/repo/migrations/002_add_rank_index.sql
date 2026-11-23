-- 002_add_rank_index.sql
BEGIN;
CREATE INDEX idx_player_name ON player (name);
COMMIT;