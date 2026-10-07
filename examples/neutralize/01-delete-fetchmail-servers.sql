-- Remove configured incoming mail servers from a restored development/test database.
-- The fetchmail module is optional, so do nothing when its table is not present.
DO $$
BEGIN
    IF to_regclass('fetchmail_server') IS NOT NULL THEN
        EXECUTE 'DELETE FROM fetchmail_server';
    END IF;
END
$$;
