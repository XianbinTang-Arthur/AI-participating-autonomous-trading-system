-- Migration 003: execution truth dedicated columns
-- Golden path P1: 把仍然停在 JSON payload 里的 execution truth 提升为硬列，
-- 供 operator/control-plane/review 直接消费，不再长期依赖 payload flatten。
--
-- 范围：
--   execution_orders.execution_style
--   execution_fills.fee_rate
--   execution_fills.exec_type
--
-- 兼容性：
--   - 表可能尚未由 create_all 建出，需先检查
--   - 旧行若 payload 中存在 truth，则做幂等回填
--   - payload 缺字段的旧行保持 NULL，不造假

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'execution_orders'
          AND table_schema = current_schema()
    ) THEN
        ALTER TABLE execution_orders
            ADD COLUMN IF NOT EXISTS execution_style VARCHAR(32);

        UPDATE execution_orders
        SET execution_style = COALESCE(
            execution_style,
            NULLIF(raw_payload ->> 'execution_style', ''),
            NULLIF(raw_payload -> 'intent' ->> 'execution_style', ''),
            NULLIF(raw_payload -> 'fill_event' ->> 'execution_style', ''),
            NULLIF(raw_payload -> 'order_state' ->> 'execution_style', '')
        )
        WHERE execution_style IS NULL;
    END IF;
END
$$;

DO $$
BEGIN
    IF EXISTS (
        SELECT 1 FROM information_schema.tables
        WHERE table_name = 'execution_fills'
          AND table_schema = current_schema()
    ) THEN
        ALTER TABLE execution_fills
            ADD COLUMN IF NOT EXISTS fee_rate VARCHAR(32),
            ADD COLUMN IF NOT EXISTS exec_type VARCHAR(16);

        UPDATE execution_fills
        SET fee_rate = COALESCE(
            fee_rate,
            NULLIF(raw_payload ->> 'fee_rate', ''),
            NULLIF(raw_payload -> 'raw_exchange' ->> 'feeRate', ''),
            NULLIF(raw_payload -> 'fill_event' -> 'raw_exchange' ->> 'feeRate', '')
        ),
            exec_type = COALESCE(
                exec_type,
                NULLIF(raw_payload ->> 'exec_type', ''),
                NULLIF(raw_payload -> 'raw_exchange' ->> 'execType', ''),
                NULLIF(raw_payload -> 'fill_event' -> 'raw_exchange' ->> 'execType', '')
            )
        WHERE fee_rate IS NULL
           OR exec_type IS NULL;
    END IF;
END
$$;
