-- context_cache_metrics: базовые SQL-шаблоны для Phase 1
-- Совместимо с SQLite/Postgres (ts хранится как unix epoch seconds).

-- 1) Общий объём за последние N часов (подставьте :since_ts)
SELECT
  COUNT(*) AS total_rows
FROM context_cache_metrics
WHERE ts >= :since_ts;

-- 2) Mode split (FULL vs DELTA_SAFE)
SELECT
  mode,
  COUNT(*) AS cnt,
  ROUND(100.0 * COUNT(*) / NULLIF((SELECT COUNT(*) FROM context_cache_metrics WHERE ts >= :since_ts), 0), 2) AS pct
FROM context_cache_metrics
WHERE ts >= :since_ts
GROUP BY mode
ORDER BY cnt DESC;

-- 3) FULL fallback reasons
SELECT
  reason,
  COUNT(*) AS cnt
FROM context_cache_metrics
WHERE ts >= :since_ts
  AND mode = 'FULL'
GROUP BY reason
ORDER BY cnt DESC;

-- 4) Ошибки провайдера по mode
SELECT
  mode,
  SUM(CASE WHEN provider_error = 1 THEN 1 ELSE 0 END) AS provider_errors,
  COUNT(*) AS total,
  ROUND(100.0 * SUM(CASE WHEN provider_error = 1 THEN 1 ELSE 0 END) / NULLIF(COUNT(*), 0), 3) AS error_rate_pct
FROM context_cache_metrics
WHERE ts >= :since_ts
GROUP BY mode
ORDER BY total DESC;

-- 5) Агрегация по модели
SELECT
  model,
  COUNT(*) AS total,
  AVG(sent_tokens) AS avg_sent_tokens,
  AVG(build_context_ms) AS avg_build_context_ms,
  SUM(CASE WHEN provider_error = 1 THEN 1 ELSE 0 END) AS provider_errors
FROM context_cache_metrics
WHERE ts >= :since_ts
GROUP BY model
ORDER BY total DESC;

-- 6) Почасовой тренд mode/reason
-- SQLite:
SELECT
  strftime('%Y-%m-%d %H:00:00', datetime(ts, 'unixepoch')) AS hour_bucket,
  mode,
  reason,
  COUNT(*) AS cnt
FROM context_cache_metrics
WHERE ts >= :since_ts
GROUP BY hour_bucket, mode, reason
ORDER BY hour_bucket DESC, cnt DESC;

-- Postgres:
-- SELECT
--   date_trunc('hour', to_timestamp(ts)) AS hour_bucket,
--   mode,
--   reason,
--   COUNT(*) AS cnt
-- FROM context_cache_metrics
-- WHERE ts >= :since_ts
-- GROUP BY hour_bucket, mode, reason
-- ORDER BY hour_bucket DESC, cnt DESC;

-- 7) p95 (Postgres only)
-- SELECT
--   percentile_cont(0.95) WITHIN GROUP (ORDER BY build_context_ms) AS p95_build_ms,
--   percentile_cont(0.95) WITHIN GROUP (ORDER BY sent_tokens) AS p95_sent_tokens
-- FROM context_cache_metrics
-- WHERE ts >= :since_ts;
