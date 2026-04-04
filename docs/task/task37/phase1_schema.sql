-- Task37 / Phase 1
-- 第一阶段最小可落地表结构
-- 目标：先支持 shadow write，不切断旧链路

create table execution_orders (
    order_id varchar(64) primary key,
    intent_id varchar(64) not null unique,
    decision_id varchar(64),
    client_order_id varchar(64) unique,
    venue_order_id varchar(64) unique,
    symbol varchar(64) not null,
    side varchar(8) not null,
    order_type varchar(16) not null,
    time_in_force varchar(16),
    requested_qty numeric(36,18) not null,
    limit_price numeric(36,18),
    product_type varchar(16) not null,
    margin_mode varchar(16) not null,
    execution_action varchar(32),
    position_intent varchar(32),
    state varchar(32) not null,
    state_version integer not null default 1,
    source_system varchar(32) not null default 'aats',
    last_exchange_ts timestamptz,
    created_at timestamptz not null,
    updated_at timestamptz not null,
    raw_payload jsonb not null default '{}'::jsonb
);

create index ix_execution_orders_symbol_state
    on execution_orders(symbol, state);

create index ix_execution_orders_decision_created
    on execution_orders(decision_id, created_at);

create table execution_order_state_history (
    id bigserial primary key,
    order_id varchar(64) not null references execution_orders(order_id),
    from_state varchar(32),
    to_state varchar(32) not null,
    reason_code varchar(64),
    source varchar(32) not null,
    source_message_id varchar(128),
    payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null
);

create index ix_execution_order_state_history_order
    on execution_order_state_history(order_id, id);

create table execution_commands (
    command_id varchar(64) primary key,
    order_id varchar(64) not null references execution_orders(order_id),
    command_type varchar(16) not null,
    idempotency_key varchar(128) not null unique,
    state varchar(16) not null,
    attempt_count integer not null default 0,
    last_error text,
    command_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null,
    updated_at timestamptz not null
);

create index ix_execution_commands_order_state
    on execution_commands(order_id, state);

create table execution_fills (
    fill_id varchar(64) primary key,
    venue_fill_id varchar(128),
    order_id varchar(64) not null references execution_orders(order_id),
    venue_order_id varchar(64),
    client_order_id varchar(64),
    decision_id varchar(64),
    intent_id varchar(64),
    symbol varchar(64) not null,
    side varchar(8) not null,
    fill_qty numeric(36,18) not null,
    fill_price numeric(36,18) not null,
    fee_amount numeric(36,18) not null default 0,
    fee_currency varchar(16),
    liquidity_role varchar(16),
    exchange_ts timestamptz not null,
    ingestion_ts timestamptz not null,
    source varchar(32) not null,
    raw_payload jsonb not null default '{}'::jsonb,
    created_at timestamptz not null,
    unique (source, venue_fill_id)
);

create index ix_execution_fills_order_ts
    on execution_fills(order_id, ingestion_ts);

create index ix_execution_fills_symbol_ts
    on execution_fills(symbol, ingestion_ts);

create table ledger_accounts (
    account_id varchar(64) primary key,
    account_type varchar(32) not null,
    currency varchar(16) not null,
    product_type varchar(16) not null,
    margin_mode varchar(16) not null,
    symbol varchar(64),
    created_at timestamptz not null
);

create unique index ux_ledger_accounts_identity
    on ledger_accounts(account_type, currency, product_type, margin_mode, coalesce(symbol, ''));

create table ledger_journals (
    journal_id varchar(64) primary key,
    journal_type varchar(32) not null,
    source_type varchar(32) not null,
    source_id varchar(64) not null,
    status varchar(16) not null,
    created_at timestamptz not null,
    posted_at timestamptz,
    metadata jsonb not null default '{}'::jsonb,
    unique (source_type, source_id)
);

create index ix_ledger_journals_status_created
    on ledger_journals(status, created_at);

create table ledger_entries (
    entry_id varchar(64) primary key,
    journal_id varchar(64) not null references ledger_journals(journal_id),
    account_id varchar(64) not null references ledger_accounts(account_id),
    direction varchar(8) not null check (direction in ('debit', 'credit')),
    amount numeric(36,18) not null check (amount >= 0),
    currency varchar(16) not null,
    effective_at timestamptz not null,
    source_type varchar(32) not null,
    source_id varchar(64) not null,
    created_at timestamptz not null
);

create index ix_ledger_entries_journal
    on ledger_entries(journal_id);

create index ix_ledger_entries_account_effective
    on ledger_entries(account_id, effective_at, created_at);

create table reservations (
    reservation_id varchar(64) primary key,
    order_id varchar(64) not null unique references execution_orders(order_id),
    reserve_account_id varchar(64) not null references ledger_accounts(account_id),
    reserved_amount numeric(36,18) not null,
    consumed_amount numeric(36,18) not null default 0,
    released_amount numeric(36,18) not null default 0,
    state varchar(16) not null,
    created_at timestamptz not null,
    updated_at timestamptz not null
);

create table settlements (
    settlement_id varchar(64) primary key,
    fill_id varchar(64) not null unique references execution_fills(fill_id),
    order_id varchar(64) not null references execution_orders(order_id),
    journal_id varchar(64) unique references ledger_journals(journal_id),
    state varchar(16) not null,
    created_at timestamptz not null,
    posted_at timestamptz
);

create table external_event_inbox (
    inbox_id varchar(64) primary key,
    source_system varchar(32) not null,
    dedupe_key varchar(256) not null unique,
    payload jsonb not null,
    received_at timestamptz not null,
    processed_at timestamptz,
    processing_result varchar(16),
    last_error text
);

create index ix_external_event_inbox_source_received
    on external_event_inbox(source_system, received_at);

create table command_outbox (
    event_id varchar(64) primary key,
    aggregate_type varchar(32) not null,
    aggregate_id varchar(64) not null,
    topic varchar(128) not null,
    payload jsonb not null,
    status varchar(16) not null,
    attempt_count integer not null default 0,
    last_error text,
    created_at timestamptz not null,
    published_at timestamptz
);

create index ix_command_outbox_status_created
    on command_outbox(status, created_at);
