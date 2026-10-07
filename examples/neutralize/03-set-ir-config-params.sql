-- Override environment-specific Odoo system parameters for a local/test database.
-- Adapt these values to the URL used by your own test environment.
INSERT INTO ir_config_parameter (key, value)
VALUES
    ('web.base.url', 'http://localhost:8069'),
    ('web.base.url.freeze', 'True')
ON CONFLICT (key) DO UPDATE
SET value = EXCLUDED.value;
