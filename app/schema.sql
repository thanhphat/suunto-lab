-- Suunto Lab schema. Idempotent: safe to run on every start.
-- Three layers:
--   raw        fit_files                       (original bytes on disk, reprocessable)
--   canonical  activities, laps, samples, rr   (vendor-neutral, from FIT)
--   derived    activity_metrics, splits, daily_load (recomputable from canonical + settings)

-- TimescaleDB is optional: present in the Docker image, absent on a plain Postgres.
DO $$ BEGIN
  CREATE EXTENSION IF NOT EXISTS timescaledb;
EXCEPTION WHEN OTHERS THEN
  RAISE NOTICE 'timescaledb not available, samples stays a plain table';
END $$;

CREATE TABLE IF NOT EXISTS athletes (
  id          bigserial PRIMARY KEY,
  name        text NOT NULL,
  created_at  timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS athlete_settings (
  athlete_id   bigint PRIMARY KEY REFERENCES athletes(id) ON DELETE CASCADE,
  hr_max       smallint NOT NULL DEFAULT 190,
  hr_rest      smallint NOT NULL DEFAULT 55,
  -- lower bound of Z1..Z5 as a fraction of hr_max
  zone_floors  real[]   NOT NULL DEFAULT '{0.5,0.6,0.7,0.8,0.9}',
  timezone     text     NOT NULL DEFAULT 'Asia/Ho_Chi_Minh',
  updated_at   timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS fit_files (
  id             bigserial PRIMARY KEY,
  athlete_id     bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  sha256         text   NOT NULL,
  source         text   NOT NULL DEFAULT 'upload',      -- upload | shortcut | suunto_api | garmin_api | coros_api
  original_name  text,
  size_bytes     integer NOT NULL,
  storage_path   text   NOT NULL,
  status         text   NOT NULL DEFAULT 'pending'
                 CHECK (status IN ('pending','processing','done','failed','duplicate')),
  error          text,
  attempts       smallint NOT NULL DEFAULT 0,
  uploaded_at    timestamptz NOT NULL DEFAULT now(),
  processed_at   timestamptz,
  UNIQUE (athlete_id, sha256)
);
CREATE INDEX IF NOT EXISTS fit_files_queue ON fit_files (uploaded_at) WHERE status = 'pending';

CREATE TABLE IF NOT EXISTS activities (
  id                bigserial PRIMARY KEY,
  athlete_id        bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  fit_file_id       bigint UNIQUE REFERENCES fit_files(id) ON DELETE SET NULL,
  start_time        timestamptz NOT NULL,
  local_date        date NOT NULL,             -- start date in the athlete's timezone
  sport             text NOT NULL,             -- FIT sport: running, cycling, hiking, ...
  sub_sport         text,                      -- FIT sub_sport: trail, treadmill, ...
  name              text,
  device_manufacturer text,
  device_product    text,
  device_serial     text,
  distance_m        real,
  elapsed_s         integer,                   -- wall clock, start to stop
  timer_s           integer,                   -- watch timer (excludes manual pauses)
  moving_s          integer,                   -- our own: speed >= 0.5 m/s
  ascent_m          real,
  descent_m         real,
  alt_min_m         real,
  alt_max_m         real,
  hr_avg            smallint,
  hr_max            smallint,
  cadence_avg       smallint,
  power_avg         smallint,
  temp_avg_c        real,
  calories          integer,
  start_lat         double precision,
  start_lon         double precision,
  has_gps           boolean NOT NULL DEFAULT false,
  created_at        timestamptz NOT NULL DEFAULT now(),
  UNIQUE (athlete_id, start_time, sport)
);
CREATE INDEX IF NOT EXISTS activities_by_date ON activities (athlete_id, start_time DESC);

CREATE TABLE IF NOT EXISTS laps (
  activity_id   bigint NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  lap_index     smallint NOT NULL,
  start_time    timestamptz,
  elapsed_s     integer,
  timer_s       integer,
  distance_m    real,
  ascent_m      real,
  descent_m     real,
  hr_avg        smallint,
  hr_max        smallint,
  cadence_avg   smallint,
  PRIMARY KEY (activity_id, lap_index)
);

-- One row per recorded sample (usually 1 s). The big table.
CREATE TABLE IF NOT EXISTS samples (
  activity_id   bigint NOT NULL,
  t             timestamptz NOT NULL,
  elapsed_s     integer NOT NULL,              -- seconds since activity start
  distance_m    real,
  lat           double precision,
  lon           double precision,
  altitude_m    real,                          -- smoothed
  hr            smallint,
  speed_ms      real,
  cadence       smallint,
  power         smallint,
  temperature_c real,
  grade         real,                          -- smoothed, fraction (0.1 = 10 %)
  moving        boolean
);
CREATE INDEX IF NOT EXISTS samples_by_activity ON samples (activity_id, t);
DO $$ BEGIN
  IF EXISTS (SELECT 1 FROM pg_extension WHERE extname = 'timescaledb') THEN
    PERFORM create_hypertable('samples', 't', chunk_time_interval => interval '30 days', if_not_exists => true);
    BEGIN
      ALTER TABLE samples SET (timescaledb.compress, timescaledb.compress_segmentby = 'activity_id', timescaledb.compress_orderby = 't');
      PERFORM add_compression_policy('samples', interval '60 days', if_not_exists => true);
    EXCEPTION WHEN OTHERS THEN RAISE NOTICE 'compression not configured: %', SQLERRM;
    END;
  END IF;
END $$;

-- Beat-to-beat intervals (Suunto records these when HRV is on). Raw order, milliseconds.
CREATE TABLE IF NOT EXISTS rr_intervals (
  activity_id  bigint PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
  rr_ms        real[] NOT NULL
);

CREATE TABLE IF NOT EXISTS activity_metrics (
  activity_id        bigint PRIMARY KEY REFERENCES activities(id) ON DELETE CASCADE,
  avg_pace_s_km      real,          -- moving time / distance
  gap_distance_m     real,          -- flat-equivalent distance (Minetti 2002 cost of running)
  gap_pace_s_km      real,
  climb_s            integer,       -- moving time on grade > 5 %
  climb_m            real,
  vam_m_h            real,          -- climb_m / climb_s, vertical metres per hour
  hr_hist            jsonb,         -- {"bpm": seconds}; lets zones be recomputed for any hr_max
  zone_s             integer[],     -- seconds in Z1..Z5 under current settings
  trimp              real,          -- Edwards TRIMP under current settings
  decoupling_pct     real,          -- pace:HR drift, 2nd half vs 1st half (aerobic durability)
  computed_at        timestamptz NOT NULL DEFAULT now()
);

CREATE TABLE IF NOT EXISTS splits (
  activity_id   bigint NOT NULL REFERENCES activities(id) ON DELETE CASCADE,
  km            smallint NOT NULL,
  distance_m    real,
  moving_s      integer,
  pace_s_km     real,
  gap_s_km      real,
  hr_avg        smallint,
  ascent_m      real,
  descent_m     real,
  PRIMARY KEY (activity_id, km)
);

-- One row per calendar day (athlete timezone), including rest days.
-- CTL = 42-day EWMA of TRIMP (fitness), ATL = 7-day EWMA (fatigue), TSB = CTL - ATL (form).
CREATE TABLE IF NOT EXISTS daily_load (
  athlete_id   bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  day          date NOT NULL,
  activities   smallint NOT NULL DEFAULT 0,
  distance_m   real NOT NULL DEFAULT 0,
  moving_s     integer NOT NULL DEFAULT 0,
  ascent_m     real NOT NULL DEFAULT 0,
  trimp        real NOT NULL DEFAULT 0,
  ctl          real,
  atl          real,
  tsb          real,
  PRIMARY KEY (athlete_id, day)
);

-- Friendly view for people and AI agents: one row per activity with metrics joined.
CREATE OR REPLACE VIEW v_activities AS
SELECT a.id, a.athlete_id, a.start_time, a.local_date, a.sport, a.sub_sport, a.name,
       round((a.distance_m / 1000)::numeric, 2)              AS distance_km,
       a.moving_s, a.elapsed_s,
       round((a.moving_s / 3600.0)::numeric, 2)              AS moving_h,
       a.ascent_m, a.descent_m, a.alt_min_m, a.alt_max_m,
       a.hr_avg, a.hr_max, a.cadence_avg, a.temp_avg_c, a.calories,
       m.avg_pace_s_km, m.gap_pace_s_km, m.vam_m_h, m.climb_m, m.climb_s,
       m.zone_s, m.trimp, m.decoupling_pct,
       a.device_manufacturer, a.device_product, a.has_gps
FROM activities a LEFT JOIN activity_metrics m ON m.activity_id = a.id;

CREATE OR REPLACE VIEW v_weekly AS
SELECT athlete_id, date_trunc('week', day)::date AS week_start,
       sum(activities)::int AS activities,
       round((sum(distance_m) / 1000)::numeric, 1) AS distance_km,
       round((sum(moving_s) / 3600.0)::numeric, 1) AS moving_h,
       round(sum(ascent_m)::numeric) AS ascent_m,
       round(sum(trimp)::numeric) AS trimp,
       round((array_agg(ctl ORDER BY day DESC))[1]::numeric, 1) AS ctl_end,
       round((array_agg(tsb ORDER BY day DESC))[1]::numeric, 1) AS tsb_end
FROM daily_load GROUP BY athlete_id, date_trunc('week', day);

-- ---------- Training notebook: exercise library, plans, journal ----------
-- Library is shared by every plan. `data` holds the full item as the web app renders it
-- (exercise: purpose, steps, cues, poses...; block: items [[exercise_id, dose]], min, note; test: unit, side, better).
CREATE TABLE IF NOT EXISTS nb_categories (
  id    text PRIMARY KEY,
  name  text NOT NULL,
  sort  smallint NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS nb_exercises (
  id          text PRIMARY KEY,
  sort        smallint NOT NULL DEFAULT 0,
  cat         text NOT NULL REFERENCES nb_categories(id),
  name        text NOT NULL,
  data        jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS nb_blocks (
  id          text PRIMARY KEY,
  sort        smallint NOT NULL DEFAULT 0,
  name        text NOT NULL,
  data        jsonb NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS nb_tests (
  id          text PRIMARY KEY,
  sort        smallint NOT NULL DEFAULT 0,
  name        text NOT NULL,
  data        jsonb NOT NULL
);

-- A plan is one goal (usually a race): weeks with goals, and a list of items per day.
CREATE TABLE IF NOT EXISTS plans (
  id            text PRIMARY KEY,
  athlete_id    bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  name          text NOT NULL,
  race          text,
  start_date    date NOT NULL,
  end_date      date NOT NULL CHECK (end_date >= start_date),
  test_note     text,
  test_targets  jsonb NOT NULL DEFAULT '{}',   -- {test_id: [target week 1, week 2, ...]}
  created_at    timestamptz NOT NULL DEFAULT now(),
  updated_at    timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE IF NOT EXISTS plan_weeks (
  plan_id     text NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  n           smallint NOT NULL,
  start_date  date NOT NULL,
  end_date    date NOT NULL,
  name        text NOT NULL,
  focus       text,
  PRIMARY KEY (plan_id, n)
);
CREATE TABLE IF NOT EXISTS plan_goals (
  id       bigserial PRIMARY KEY,
  plan_id  text NOT NULL,
  week_n   smallint NOT NULL,
  idx      smallint NOT NULL,
  cat      text NOT NULL CHECK (cat IN ('test', 'volume', 'habit')),
  text     text NOT NULL,
  done_at  timestamptz,
  UNIQUE (plan_id, week_n, idx),
  FOREIGN KEY (plan_id, week_n) REFERENCES plan_weeks(plan_id, n) ON DELETE CASCADE
);
-- slot: S sáng, T trưa, C chiều/tối, N mỗi ngày. ref: '#BLOCK' (nb_blocks) or '@exercise' (nb_exercises) or ''.
CREATE TABLE IF NOT EXISTS plan_items (
  id       bigserial PRIMARY KEY,
  plan_id  text NOT NULL REFERENCES plans(id) ON DELETE CASCADE,
  day      date NOT NULL,
  idx      smallint NOT NULL,
  slot     char(1) NOT NULL CHECK (slot IN ('S', 'T', 'C', 'N')),
  label    text NOT NULL DEFAULT '',
  ref      text NOT NULL DEFAULT '',
  done_at  timestamptz,
  UNIQUE (plan_id, day, idx)
);

-- Journal: belongs to the athlete and the day, not to a plan, so it carries across goals.
CREATE TABLE IF NOT EXISTS daily_notes (
  athlete_id  bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  day         date NOT NULL,
  note        text NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (athlete_id, day)
);
-- Weekly self-tests. side: L / R for two-sided tests, V otherwise. Pace tests store minutes per km.
CREATE TABLE IF NOT EXISTS test_results (
  athlete_id  bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  test_id     text NOT NULL REFERENCES nb_tests(id),
  day         date NOT NULL,
  side        char(1) NOT NULL CHECK (side IN ('L', 'R', 'V')),
  value       double precision NOT NULL,
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (athlete_id, test_id, day, side)
);
-- Coach feedback written by Claude (or a human). kind 'week' is keyed by the plan week's start date.
CREATE TABLE IF NOT EXISTS coach_notes (
  athlete_id  bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  kind        text NOT NULL CHECK (kind IN ('day', 'week')),
  day         date NOT NULL,
  body        text NOT NULL,
  author      text NOT NULL DEFAULT 'claude',
  updated_at  timestamptz NOT NULL DEFAULT now(),
  PRIMARY KEY (athlete_id, kind, day)
);

-- Race calendar, independent of plans: registered or planned races, and results afterwards.
-- priority A = goal race with its own plan, B = training race inside an A plan, C = for fun.
CREATE TABLE IF NOT EXISTS races (
  id             bigserial PRIMARY KEY,
  athlete_id     bigint NOT NULL REFERENCES athletes(id) ON DELETE CASCADE,
  name           text NOT NULL,
  race_date      date NOT NULL,
  location       text,
  distance_km    real CHECK (distance_km > 0),
  ascent_m       real CHECK (ascent_m >= 0),
  cutoff_h       real CHECK (cutoff_h > 0),
  start_time     time,
  priority       char(1) NOT NULL DEFAULT 'B' CHECK (priority IN ('A', 'B', 'C')),
  status         text NOT NULL DEFAULT 'planned' CHECK (status IN ('planned', 'registered', 'done', 'dnf', 'dns')),
  register_by    date,                          -- registration deadline
  change_by      date,                          -- last day to change distance
  url            text,
  notes          text,
  plan_id        text REFERENCES plans(id) ON DELETE SET NULL,
  result_time_s  integer CHECK (result_time_s > 0),   -- official time; else taken from the watch
  result_note    text,
  created_at     timestamptz NOT NULL DEFAULT now(),
  updated_at     timestamptz NOT NULL DEFAULT now(),
  UNIQUE (athlete_id, name, race_date)
);
CREATE INDEX IF NOT EXISTS races_by_date ON races (athlete_id, race_date);

-- Plan items with the block / exercise name resolved: one row per thing to do on a day.
CREATE OR REPLACE VIEW v_plan_days AS
SELECT i.plan_id, p.athlete_id, i.day, i.idx, i.slot, i.ref,
       coalesce(nullif(i.label, ''), b.name, e.name, i.ref) AS title,
       i.done_at IS NOT NULL AS done
FROM plan_items i JOIN plans p ON p.id = i.plan_id
LEFT JOIN nb_blocks b ON i.ref = '#' || b.id
LEFT JOIN nb_exercises e ON i.ref = '@' || e.id;

-- Read-only role for AI agents (query_sql). Password set by the app at startup.
DO $$ BEGIN
  IF NOT EXISTS (SELECT 1 FROM pg_roles WHERE rolname = 'agent_ro') THEN
    CREATE ROLE agent_ro LOGIN;
  END IF;
END $$;
GRANT USAGE ON SCHEMA public TO agent_ro;
REVOKE ALL ON ALL TABLES IN SCHEMA public FROM agent_ro;
GRANT SELECT ON athletes, athlete_settings, activities, laps, samples, rr_intervals,
                activity_metrics, splits, daily_load, v_activities, v_weekly,
                nb_categories, nb_exercises, nb_blocks, nb_tests, plans, plan_weeks, plan_goals, plan_items,
                daily_notes, test_results, coach_notes, races, v_plan_days TO agent_ro;
ALTER ROLE agent_ro SET statement_timeout = '8s';
ALTER ROLE agent_ro SET default_transaction_read_only = on;
