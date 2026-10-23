import threading

import pytest

from app.models.download_job import (DownloadJob, InvalidTransition, JobKind, JobQueue, JobStatus)


def job(kind=JobKind.CLIP, **kw):
    return DownloadJob(kind, kw.pop("name", "j"), **kw)


def test_legal_lifecycle():
    j = job()
    for s in (JobStatus.DOWNLOADING, JobStatus.TRIMMING, JobStatus.CROPPING, JobStatus.COMPLETED):
        j.transition(s)
    assert j.progress == 100.0 and j.is_finished


@pytest.mark.parametrize("start,bad", [
    (JobStatus.COMPLETED, JobStatus.WAITING), (JobStatus.COMPLETED, JobStatus.DOWNLOADING),
    (JobStatus.FAILED, JobStatus.COMPLETED), (JobStatus.CANCELED, JobStatus.TRIMMING),
    (JobStatus.WAITING, JobStatus.COMPLETED),
])
def test_illegal_transitions(start, bad):
    j = job()
    j.status = start
    with pytest.raises(InvalidTransition):
        j.transition(bad)


def test_concurrency_limit_and_order():
    q = JobQueue(max_concurrent=2)
    jobs = [q.add(job(name=str(i))) for i in range(4)]
    for _ in range(2):
        nxt = q.next_runnable()
        nxt.transition(JobStatus.DOWNLOADING)
    assert q.next_runnable() is None                       # limit reached
    assert [j.status for j in jobs[:2]] == [JobStatus.DOWNLOADING] * 2
    jobs[0].transition(JobStatus.COMPLETED)
    assert q.next_runnable() is jobs[2]                    # FIFO


def test_dependency_gating():
    q = JobQueue(4)
    src = q.add(job(JobKind.SOURCE))
    clip = q.add(job(depends_on=src.id))
    assert q.next_runnable() is src
    src.transition(JobStatus.DOWNLOADING)
    assert q.next_runnable() is None                       # clip must wait for the source
    src.transition(JobStatus.COMPLETED)
    assert q.next_runnable() is clip


def test_failed_dependency_blocks_dependants_and_retry_recovers():
    q = JobQueue(4)
    src = q.add(job(JobKind.SOURCE))
    clip = q.add(job(depends_on=src.id))
    src.transition(JobStatus.DOWNLOADING)
    src.transition(JobStatus.FAILED)
    assert q.blocked_jobs() == [clip]
    q.retry(src.id)
    assert src.status == JobStatus.WAITING and q.blocked_jobs() == []


def test_completed_clips_survive_failure_of_another():
    q = JobQueue(4)
    a, b, c = (q.add(job(name=n)) for n in "abc")
    a.transition(JobStatus.TRIMMING)
    a.transition(JobStatus.COMPLETED)
    b.transition(JobStatus.TRIMMING)
    b.transition(JobStatus.FAILED)
    c.transition(JobStatus.TRIMMING)
    c.transition(JobStatus.COMPLETED)
    assert q.summary() == {"Completed": 2, "Failed": 1}
    q.retry(b.id)                                          # only the failed one is re-queued
    assert (a.status, c.status, b.status) == (JobStatus.COMPLETED, JobStatus.COMPLETED, JobStatus.WAITING)
    with pytest.raises(InvalidTransition):
        q.retry(a.id)


def test_cancel_waiting_and_running():
    q = JobQueue(2)
    w, r = q.add(job()), q.add(job())
    r.transition(JobStatus.DOWNLOADING)
    q.cancel(w.id)
    assert w.status == JobStatus.CANCELED
    q.cancel(r.id)
    assert r.cancel_event.is_set() and r.cancel_requested and r.status == JobStatus.DOWNLOADING  # worker stops it
    r.transition(JobStatus.CANCELED)
    q.retry(r.id)
    assert not r.cancel_event.is_set() and not r.cancel_requested


def test_pause_blocks_new_starts_only():
    q = JobQueue(2)
    q.add(job())
    q.paused = True
    assert q.next_runnable() is None
    q.paused = False
    assert q.next_runnable() is not None


def test_remove_only_finished():
    q = JobQueue()
    j = q.add(job())
    with pytest.raises(InvalidTransition):
        q.remove(j.id)
    j.transition(JobStatus.CANCELED)
    q.remove(j.id)
    assert q.jobs == []
    k = q.add(job())
    k.transition(JobStatus.FAILED)
    assert q.clear_finished() == 1


def test_unmeasurable_progress_is_none_not_fabricated():
    j = job()
    assert j.progress is None and j.speed is None and j.eta is None
