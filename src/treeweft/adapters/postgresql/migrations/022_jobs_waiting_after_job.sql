-- Refresh preemption (ADR-003, code-review finding #6): a job created while
-- its source's `resummarize` refresh is RUNNING is persisted with
-- status='waiting' and payload->>'after_job' = <refresh job id>, deliberately
-- left out of job_queue and out of jobs_active_source_uniq's status set
-- (migration 008 only covers 'queued'/'running'). The worker's post-job hook
-- and startup recovery both need to find a job's waiting followers quickly,
-- so index the lookup rather than scan every 'waiting' row.
CREATE INDEX IF NOT EXISTS jobs_waiting_after_job_idx
    ON jobs ((payload ->> 'after_job'))
    WHERE status = 'waiting';
