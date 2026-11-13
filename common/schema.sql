-- 报关 RPA 仿真系统共享数据库（SQLite）
PRAGMA journal_mode=WAL;

-- 通用代码表：kind 区分币制/计量单位/HS编码/国别港口等
CREATE TABLE IF NOT EXISTS code_table(
  kind TEXT NOT NULL,
  code TEXT NOT NULL,
  name TEXT NOT NULL,
  extra TEXT,                       -- JSON 扩展属性，如港口所属国别、HS法定单位
  PRIMARY KEY(kind, code)
);

-- 机器人账号（仿真网站登录账号）
CREATE TABLE IF NOT EXISTS accounts(
  id INTEGER PRIMARY KEY,
  username TEXT UNIQUE NOT NULL,
  password TEXT NOT NULL,
  enabled INTEGER DEFAULT 1
);

-- 全局键值设置
CREATE TABLE IF NOT EXISTS settings(
  key TEXT PRIMARY KEY,
  value TEXT
);

-- 录入任务
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY,
  ref_no TEXT UNIQUE,
  inbox_excel TEXT,
  inbox_pdf TEXT,
  status TEXT NOT NULL,             -- imported/invalid/queued/running/submitted/verified/mismatch/failed/done
  defect_type TEXT,                 -- 脏数据类型（生成器标记）
  validation_errors TEXT,           -- JSON 校验错误列表
  assigned_worker TEXT,
  lock_token TEXT,
  locked_at TEXT,
  run_count INTEGER DEFAULT 0,
  created_at TEXT,
  updated_at TEXT
);

-- 解析后的统一报关单结构
CREATE TABLE IF NOT EXISTS declarations(
  task_id INTEGER PRIMARY KEY,
  ref_no TEXT,
  header_json TEXT,
  items_json TEXT,
  source_hash TEXT,
  updated_at TEXT
);

-- 断点续录进度
CREATE TABLE IF NOT EXISTS task_progress(
  task_id INTEGER PRIMARY KEY,
  stage TEXT,                       -- header/items/submit/verify/done
  item_index INTEGER DEFAULT 0,     -- 已录完的表体行数
  draft_id TEXT,                    -- 仿真网站草稿ID
  sim_ref TEXT,                     -- 仿真网站申报回执编号
  updated_at TEXT
);

-- 仿真网站服务端会话
CREATE TABLE IF NOT EXISTS sim_sessions(
  sid TEXT PRIMARY KEY,
  account TEXT,
  created_at TEXT,
  last_seen TEXT
);
CREATE TABLE IF NOT EXISTS sim_captcha(
  sid TEXT PRIMARY KEY,
  text TEXT,
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS sim_drafts(
  id INTEGER PRIMARY KEY,
  account TEXT,
  ref_no TEXT,
  header_json TEXT,
  items_json TEXT,
  saved_at TEXT
);
CREATE TABLE IF NOT EXISTS sim_declarations(
  sim_ref TEXT PRIMARY KEY,
  ref_no TEXT,
  account TEXT,
  header_json TEXT,
  items_json TEXT,
  submitted_at TEXT
);

-- 机器人监控
CREATE TABLE IF NOT EXISTS workers(
  id TEXT PRIMARY KEY,
  name TEXT,
  account TEXT,
  status TEXT,                      -- idle/running/stopped/error
  heartbeat TEXT,
  current_task INTEGER,
  info TEXT
);

-- 截图留痕
CREATE TABLE IF NOT EXISTS screenshots(
  id INTEGER PRIMARY KEY,
  task_id INTEGER,
  step TEXT,
  path TEXT,
  created_at TEXT
);

-- 回查核对报告
CREATE TABLE IF NOT EXISTS reconcile_reports(
  task_id INTEGER PRIMARY KEY,
  result TEXT,                      -- match/mismatch
  diffs_json TEXT,
  sim_json TEXT,
  created_at TEXT
);

-- 异常工单
CREATE TABLE IF NOT EXISTS exceptions(
  id INTEGER PRIMARY KEY,
  task_id INTEGER,
  kind TEXT,
  message TEXT,
  status TEXT DEFAULT 'open',       -- open/resolved
  resolution TEXT,
  created_at TEXT,
  resolved_at TEXT
);

CREATE TABLE IF NOT EXISTS audit_log(
  id INTEGER PRIMARY KEY,
  ts TEXT,
  actor TEXT,
  action TEXT,
  detail TEXT
);
