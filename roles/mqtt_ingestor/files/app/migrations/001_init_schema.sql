-- =============================================================================
-- 001_init_schema.sql
-- Idempotent T-SQL migration for the mqtt-ingestor schema.
-- All objects are guarded with IF NOT EXISTS / IF OBJECT_ID checks.
-- Run via DbWriter.run_migrations() or manually in SSMS.
-- Batches are separated by GO so the runner can split and execute them.
-- =============================================================================

-- ---------------------------------------------------------------------------
-- ingest schema (operational tables)
-- ---------------------------------------------------------------------------

IF NOT EXISTS (SELECT 1 FROM sys.schemas WHERE name = 'ingest')
    EXEC('CREATE SCHEMA ingest')
GO

-- Table: pipeline_state  (key/value store)
IF NOT EXISTS (
    SELECT 1 FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'ingest' AND t.name = 'pipeline_state'
)
BEGIN
    CREATE TABLE [ingest].[pipeline_state] (
        state_key   VARCHAR(80)      NOT NULL,
        state_value NVARCHAR(2000)   NOT NULL,
        updated_utc DATETIME2(3)     NOT NULL  DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_pipeline_state PRIMARY KEY CLUSTERED (state_key)
    );

    INSERT INTO [ingest].[pipeline_state] (state_key, state_value)
    VALUES
        ('schema_version',    '1'),
        ('last_spool_offset', '0'),
        ('last_db_write_utc', ''),
        ('last_error_utc',    '');
END
GO

-- Table: telemetry_deadletter
IF NOT EXISTS (
    SELECT 1 FROM sys.tables t
    JOIN sys.schemas s ON t.schema_id = s.schema_id
    WHERE s.name = 'ingest' AND t.name = 'telemetry_deadletter'
)
BEGIN
    CREATE TABLE [ingest].[telemetry_deadletter] (
        deadletter_id        BIGINT          IDENTITY(1,1)  NOT NULL,
        first_seen_utc       DATETIME2(3)                   NOT NULL,
        moved_utc            DATETIME2(3)                   NOT NULL  DEFAULT SYSUTCDATETIME(),
        mqtt_topic           NVARCHAR(300)                  NOT NULL,
        payload_hash_sha256  CHAR(64)                       NOT NULL,
        payload_text         NVARCHAR(MAX)                  NULL,
        payload_json         NVARCHAR(MAX)                  NULL,
        failure_reason       NVARCHAR(1000)                 NULL,
        retry_count          INT                            NOT NULL  DEFAULT 0,
        source_timestamp_utc DATETIME2(3)                   NULL,
        source_id            NVARCHAR(120)                  NULL,
        CONSTRAINT PK_telemetry_deadletter PRIMARY KEY CLUSTERED (deadletter_id)
    );

    CREATE NONCLUSTERED INDEX IX_deadletter_moved_utc
        ON [ingest].[telemetry_deadletter] (moved_utc);

    CREATE NONCLUSTERED INDEX IX_deadletter_topic
        ON [ingest].[telemetry_deadletter] (mqtt_topic);
END
GO

-- ---------------------------------------------------------------------------
-- dbo schema safety-net guards (tables are expected to exist already;
-- these CREATE blocks fire only if a fresh DB is used for testing)
-- ---------------------------------------------------------------------------

IF OBJECT_ID(N'[dbo].[devices]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[devices] (
        id            INT             IDENTITY(1,1) NOT NULL,
        serial_number NVARCHAR(50)                  NULL,
        customer      NVARCHAR(100)                 NOT NULL,
        location      NVARCHAR(100)                 NOT NULL,
        machine_name  NVARCHAR(100)                 NOT NULL,
        created_at    DATETIME2                     NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at    DATETIME2                     NULL,
        CONSTRAINT PK_devices PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_devices_serial UNIQUE (serial_number),
        CONSTRAINT UQ_devices_customer_location_machine UNIQUE (customer, location, machine_name)
    );
END
GO

IF OBJECT_ID(N'[dbo].[device_status]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_status] (
        id            INT         IDENTITY(1,1) NOT NULL,
        device_id     INT         NOT NULL,
        status        NVARCHAR(20)              NULL,
        ts_epoch      BIGINT                    NULL,
        ts_datetime   DATETIME2                 NULL,
        offline_since DATETIME2                 NULL,
        created_at    DATETIME2                 NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at    DATETIME2                 NULL,
        CONSTRAINT PK_device_status PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_device_status_device UNIQUE (device_id),
        CONSTRAINT FK_device_status_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
END
GO

IF OBJECT_ID(N'[dbo].[device_application_status]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_application_status] (
        id                  INT     IDENTITY(1,1) NOT NULL,
        device_id           INT     NOT NULL,
        application_running BIT                   NULL,
        ts_epoch            BIGINT                NULL,
        ts_datetime         DATETIME2             NULL,
        stopped_since       DATETIME2             NULL,
        created_at          DATETIME2             NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at          DATETIME2             NULL,
        CONSTRAINT PK_device_app_status PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_device_app_status_device UNIQUE (device_id),
        CONSTRAINT FK_device_app_status_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
END
GO

IF OBJECT_ID(N'[dbo].[device_os_status]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_os_status] (
        id          INT           IDENTITY(1,1) NOT NULL,
        device_id   INT           NOT NULL,
        os_version  NVARCHAR(200) NULL,
        ts_epoch    BIGINT        NULL,
        ts_datetime DATETIME2     NULL,
        created_at  DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at  DATETIME2     NULL,
        CONSTRAINT PK_device_os_status PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_device_os_status_device UNIQUE (device_id),
        CONSTRAINT FK_device_os_status_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
END
GO

IF OBJECT_ID(N'[dbo].[device_uptime_status]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_uptime_status] (
        id             INT             IDENTITY(1,1) NOT NULL,
        device_id      INT             NOT NULL,
        uptime_seconds DECIMAL(18,3)   NULL,
        ts_epoch       BIGINT          NULL,
        ts_datetime    DATETIME2       NULL,
        created_at     DATETIME2       NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at     DATETIME2       NULL,
        CONSTRAINT PK_device_uptime_status PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_device_uptime_status_device UNIQUE (device_id),
        CONSTRAINT FK_device_uptime_status_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
END
GO

IF OBJECT_ID(N'[dbo].[device_storage_status]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_storage_status] (
        id            INT           IDENTITY(1,1) NOT NULL,
        device_id     INT           NOT NULL,
        drive         NVARCHAR(10)  NOT NULL,
        drive_type    NVARCHAR(50)  NULL,
        format        NVARCHAR(20)  NULL,
        total_gb      DECIMAL(10,2) NULL,
        free_gb       DECIMAL(10,2) NULL,
        used_gb       DECIMAL(10,2) NULL,
        usage_percent DECIMAL(5,2)  NULL,
        ts_epoch      BIGINT        NULL,
        ts_datetime   DATETIME2     NULL,
        created_at    DATETIME2     NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at    DATETIME2     NULL,
        CONSTRAINT PK_device_storage_status PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_device_storage_device_drive UNIQUE (device_id, drive),
        CONSTRAINT FK_device_storage_status_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
END
GO

IF OBJECT_ID(N'[dbo].[device_statistics]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_statistics] (
        id                BIGINT  IDENTITY(1,1) NOT NULL,
        device_id         INT     NOT NULL,
        ts_epoch          BIGINT  NULL,
        ts_datetime       DATETIME2             NULL,
        total_items       INT     NULL,
        no_read           INT     NULL,
        good_read         INT     NULL,
        no_dimension      INT     NULL,
        no_weight         INT     NULL,
        data_sent         INT     NULL,
        not_sent          INT     NULL,
        image_sent        INT     NULL,
        image_not_sent    INT     NULL,
        item_out_of_spec  INT     NULL,
        more_than_1_item  INT     NULL,
        hand_scanned      INT     NULL,
        complete          INT     NULL,
        created_at        DATETIME2 NOT NULL DEFAULT SYSUTCDATETIME(),
        CONSTRAINT PK_device_statistics PRIMARY KEY CLUSTERED (id),
        CONSTRAINT FK_device_statistics_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
    CREATE NONCLUSTERED INDEX IX_device_statistics_device_ts
        ON [dbo].[device_statistics] (device_id, ts_datetime);
END
GO

-- ---------------------------------------------------------------------------
-- New table: device_os_metrics
-- One row per device, MERGE-updated by OS/cpu, OS/memory, OS/temperature topics.
-- ---------------------------------------------------------------------------

IF OBJECT_ID(N'[dbo].[device_os_metrics]', N'U') IS NULL
BEGIN
    CREATE TABLE [dbo].[device_os_metrics] (
        id            INT            IDENTITY(1,1) NOT NULL,
        device_id     INT            NOT NULL,
        cpu_percent   DECIMAL(5,2)   NULL,
        mem_total_gb  DECIMAL(10,2)  NULL,
        mem_used_gb   DECIMAL(10,2)  NULL,
        mem_free_gb   DECIMAL(10,2)  NULL,
        mem_usage_pct DECIMAL(5,2)   NULL,
        temp_celsius  DECIMAL(5,2)   NULL,
        temp_status   NVARCHAR(50)   NULL,
        cpu_ts_epoch  BIGINT         NULL,
        mem_ts_epoch  BIGINT         NULL,
        temp_ts_epoch BIGINT         NULL,
        ts_datetime   DATETIME2      NULL,
        created_at    DATETIME2      NOT NULL DEFAULT SYSUTCDATETIME(),
        updated_at    DATETIME2      NULL,
        CONSTRAINT PK_device_os_metrics PRIMARY KEY CLUSTERED (id),
        CONSTRAINT UQ_os_metrics_device UNIQUE (device_id),
        CONSTRAINT FK_os_metrics_device FOREIGN KEY (device_id) REFERENCES [dbo].[devices](id)
    );
    CREATE NONCLUSTERED INDEX IX_os_metrics_ts
        ON [dbo].[device_os_metrics] (ts_datetime);
END
GO
