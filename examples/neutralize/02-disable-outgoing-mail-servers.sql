-- Disable every configured outgoing SMTP server.
-- This is idempotent and can be run repeatedly.
UPDATE ir_mail_server
SET active = FALSE
WHERE active IS DISTINCT FROM FALSE;
