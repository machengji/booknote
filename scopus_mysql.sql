-- Scopus 交付表（列表 + 详情），与 2026_wos_records 同库同风格。
--
-- 分工：
--   * 本文件建的是**主表**，由 harvest/scopus_to_mysql.py 从 JSONL 写入，
--     并在 sync_run_log / sync_state 写 group_name='scopus-jsonl' 的记录。
--   * 甲方下游的 qiyans_sync_* / *_sync_ledger 是他们从主表往自己系统同步用的，
--     不在这里建、也不要覆盖。
--
-- 命名沿用 WoS 表的口径：刊名字段叫 source_title（不是 source）。
-- 主键用 eid：Scopus 的 eid 是文献级唯一号，一篇一行。
-- 注意 Scopus 会改 ORIG-LOAD-DATE（同一篇在“天”之间搬家），所以 load_date 会随
-- 最新一次采集更新，这不是错，不要拿它当“首次入库时间”。

-- ---------------------------------------------------------------- 列表表
CREATE TABLE IF NOT EXISTS `2026_scopus_records` (
  `eid`          VARCHAR(64)  NOT NULL COMMENT 'EID:Scopus唯一号，主键',
  `doi`          VARCHAR(256) NULL COMMENT 'DOI（已去 https://doi.org/ 前缀）',
  `title`        TEXT         NULL COMMENT '标题',
  `source_title` VARCHAR(512) NULL COMMENT '刊名/来源（列表 source 字段）',
  `source_id`    VARCHAR(32)  NULL COMMENT '来源ID',
  `issn`         VARCHAR(16)  NULL COMMENT 'ISSN（会议/书常无）',
  `year`         SMALLINT     NULL COMMENT '出版年',
  `citedby`      INT          NULL COMMENT '被引次数',
  `subtype`      VARCHAR(128) NULL COMMENT '文献子类型，ar=article',
  `authors`      TEXT         NULL COMMENT '作者缩写，分号分隔',
  `load_date`    DATE         NULL COMMENT '采集归属日（ORIG-LOAD-DATE 窗口）',
  `updated_at`   TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP
                 ON UPDATE CURRENT_TIMESTAMP COMMENT '入库更新时间',
  PRIMARY KEY (`eid`),
  KEY `idx_doi` (`doi`),
  KEY `idx_year` (`year`),
  KEY `idx_source` (`source_title`(191)),
  KEY `idx_load_date` (`load_date`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Scopus 题录列表：一篇一行，来源=按刊线/历史按天线采到的检索结果';

-- ---------------------------------------------------------------- 详情表
-- 标量字段拆成列（可查可筛），嵌套结构存 JSON 原文（作者/机构/关键词/基金/通讯/PubMed）。
-- 另抽三个常用派生列：source_title / source_id / pubmed_id，便于和列表表或 WoS 表关联。
CREATE TABLE IF NOT EXISTS `2026_scopus_details` (
  `eid`                   VARCHAR(64)   NOT NULL COMMENT 'EID:Scopus唯一号，主键',
  `doi`                   VARCHAR(256)  NULL,
  `title`                 TEXT          NULL,
  `source_title`          VARCHAR(512)  NULL COMMENT '从 source.title 抽出',
  `source_id`             VARCHAR(32)   NULL COMMENT '从 source.id 抽出',
  `issn`                  VARCHAR(16)   NULL,
  `publication_year`      SMALLINT      NULL,
  `publication_stage`     VARCHAR(32)   NULL COMMENT 'FINAL / AIP 等',
  `language`              VARCHAR(64)   NULL,
  `open_access`           TINYINT       NULL COMMENT '1=开放获取',
  `volume`                VARCHAR(32)   NULL,
  `issue`                 VARCHAR(32)   NULL,
  `pages`                 VARCHAR(64)   NULL,
  `article_number`        VARCHAR(64)   NULL,
  `publisher`             VARCHAR(512)  NULL,
  `document_type`         VARCHAR(32)   NULL COMMENT 'type 字段，ar=article',
  `cited_by`              INT           NULL,
  `pubmed_id`             VARCHAR(32)   NULL COMMENT '从 pubmedInfo.id 抽出',
  `abstract_text`         MEDIUMTEXT    NULL COMMENT '摘要全文',
  `author_keywords`       TEXT          NULL COMMENT '作者关键词（JSON 数组）',
  `authors_json`          MEDIUMTEXT    NULL COMMENT '作者结构化 [{id,name,lastName,firstName,initials}]',
  `affiliations_json`     MEDIUMTEXT    NULL COMMENT '机构结构化 [{id,name,departmentId,link}]',
  `indexed_keywords_json` MEDIUMTEXT    NULL COMMENT '索引关键词（MSH/GDE 词表）',
  `funding_json`          MEDIUMTEXT    NULL COMMENT '资助信息',
  `correspondences_json`  MEDIUMTEXT    NULL COMMENT '通讯作者及其机构',
  `source_json`           TEXT          NULL COMMENT 'source 原始对象',
  `is_missing`            TINYINT       NOT NULL DEFAULT 0 COMMENT '1=网关 404，这篇没有详情',
  `load_date`             DATE          NULL COMMENT '采集归属日（与列表表对齐）',
  `fetched_at`            DATETIME      NULL COMMENT '详情抓取时间',
  `updated_at`            TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP
                          ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (`eid`),
  KEY `idx_doi` (`doi`),
  KEY `idx_source` (`source_title`(191)),
  KEY `idx_load_date` (`load_date`),
  KEY `idx_pubmed` (`pubmed_id`),
  KEY `idx_year` (`publication_year`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci
  COMMENT='Scopus 详情：摘要/机构/关键词/基金/卷期页；按 eid 与列表表一一对应';
