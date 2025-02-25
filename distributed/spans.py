from __future__ import annotations

import uuid
import weakref
from collections import defaultdict
from collections.abc import Hashable, Iterable, Iterator
from contextlib import contextmanager
from itertools import islice
from typing import TYPE_CHECKING, Any

import dask.config

from distributed.collections import sum_mappings
from distributed.itertools import ffill
from distributed.metrics import time

if TYPE_CHECKING:
    from distributed import Scheduler, Worker
    from distributed.client import SourceCode
    from distributed.scheduler import TaskGroup, TaskState, TaskStateState, WorkerState


@contextmanager
def span(*tags: str) -> Iterator[str]:
    """Tag group of tasks to be part of a certain group, called a span.

    This context manager can be nested, thus creating sub-spans. If you close and
    re-open a span context manager with the same tag, you'll end up with two separate
    spans.

    Every cluster defines a global "default" span when no span has been defined by the
    client; the default span is automatically closed and reopened when all tasks
    associated to it have been completed; in other words the cluster is idle save for
    tasks that are explicitly annotated by a span. Note that, in some edge cases, you
    may end up with overlapping default spans, e.g. if a worker crashes and all unique
    tasks that were in memory on it need to be recomputed.

    Examples
    --------
    >>> import dask.array as da
    >>> import distributed
    >>> client = distributed.Client()
    >>> with span("my workflow"):
    ...     with span("phase 1"):
    ...         a = da.random.random(10)
    ...         b = a + 1
    ...     with span("phase 2"):
    ...         c = b * 2
    ... d = c.sum()
    >>> d.compute()

    In the above example,
    - Tasks of collections a and b are annotated to belong to span
      ``('my workflow', 'phase 1')``, 'ids': (<id0>, <id1>)}``;
    - Tasks of collection c (that aren't already part of a or b) are annotated to belong
      to span ``('my workflow', 'phase 2')``;
    - Tasks of collection d (that aren't already part of a, b, or c) are *not*
      annotated but will nonetheless be attached to span ``('default', )``.

    You may also set more than one tag at once; e.g.
    >>> with span("workflow1", "version1"):
    ...     ...

    Finally, you may capture the ID of a span on the client to match it with the
    :class:`Span` objects the scheduler:
    >>> cluster = distributed.LocalCluster()
    >>> client = distributed.Client(cluster)
    >>> with span("my workflow") as span_id:
    ...     client.submit(lambda: "Hello world!").result()
    >>> span = client.cluster.scheduler.extensions["spans"].spans[span_id]

    Notes
    -----
    Spans are based on annotations, and just like annotations they can be lost during
    optimization. Set config ``optimization.fuse.active: false`` to prevent this issue.
    """
    if not tags:
        raise ValueError("Must specify at least one span tag")

    annotation = dask.get_annotations().get("span")
    prev_tags = annotation["name"] if annotation else ()
    # You must specify the full history of IDs, not just the parent, because
    # otherwise you would not be able to uniquely identify grandparents when
    # they have no tasks of their own.
    prev_ids = annotation["ids"] if annotation else ()
    ids = tuple(str(uuid.uuid4()) for _ in tags)
    with dask.annotate(span={"name": prev_tags + tags, "ids": prev_ids + ids}):
        yield ids[-1]


class Span:
    #: (<tag>, <tag>, ...)
    #: Matches ``TaskState.annotations["span"]["name"]``, both on the scheduler and the
    #: worker.
    name: tuple[str, ...]

    #: <uuid>
    #: Taken from ``TaskState.annotations["span"]["id"][-1]``.
    #: Matches ``distributed.scheduler.TaskState.group.span_id``
    #: and ``distributed.worker_state_machine.TaskState.span_id``.
    id: str

    _parent: weakref.ref[Span] | None

    #: Direct children of this span, sorted by creation time
    children: list[Span]

    #: Task groups *directly* belonging to this span.
    #:
    #: See also
    #: --------
    #  traverse_groups
    #:
    #: Notes
    #: -----
    #: TaskGroups are forgotten when the last task is forgotten. If a user calls
    #: compute() twice on the same collection, you'll have more than one group with the
    #: same tg.name in this set! For the same reason, while the same TaskGroup object is
    #: guaranteed to be attached to exactly one Span, you may have different TaskGroups
    #: with the same key attached to different Spans.
    groups: set[TaskGroup]

    #: Time when the span first appeared on the scheduler.
    #: The same property on parent spans is always less than or equal to this.
    #:
    #: See also
    #: --------
    #: start
    #: stop
    enqueued: float

    #: Source code snippets, if it was sent by the client.
    #: We're using a dict without values as an insertion-sorted set.
    _code: dict[tuple[SourceCode, ...], None]

    _cumulative_worker_metrics: defaultdict[tuple[Hashable, ...], float]

    #: reference to SchedulerState.total_nthreads_history
    _total_nthreads_history: list[tuple[float, int]]
    #: Length of total_nthreads_history when this span was enqueued
    _total_nthreads_offset: int

    # Support for weakrefs to a class with __slots__
    __weakref__: Any

    __slots__ = tuple(__annotations__)

    def __init__(
        self,
        name: tuple[str, ...],
        id_: str,
        parent: Span | None,
        total_nthreads_history: list[tuple[float, int]],
    ):
        self.name = name
        self.id = id_
        self._parent = weakref.ref(parent) if parent is not None else None
        self.enqueued = time()
        self.children = []
        self.groups = set()
        self._code = {}
        self._cumulative_worker_metrics = defaultdict(float)
        assert len(total_nthreads_history) > 0
        self._total_nthreads_history = total_nthreads_history
        self._total_nthreads_offset = len(total_nthreads_history) - 1

    def __repr__(self) -> str:
        return f"Span<name={self.name}, id={self.id}>"

    @property
    def parent(self) -> Span | None:
        if self._parent:
            out = self._parent()
            assert out
            return out
        return None

    def traverse_spans(self) -> Iterator[Span]:
        """Top-down recursion of all spans belonging to this branch off span tree,
        including self
        """
        yield self
        for child in self.children:
            yield from child.traverse_spans()

    def traverse_groups(self) -> Iterator[TaskGroup]:
        """All TaskGroups belonging to this branch of span tree"""
        for span in self.traverse_spans():
            yield from span.groups

    @property
    def start(self) -> float:
        """Earliest time when a task belonging to this span tree started computing;
        0 if no task has *finished* computing yet.

        Note
        ----
        This is not updated until at least one task has *finished* computing.
        It could move backwards as tasks complete.

        See also
        --------
        enqueued
        stop
        distributed.scheduler.TaskGroup.start
        """
        out = min(
            (tg.start for tg in self.traverse_groups() if tg.start != 0.0),
            default=0.0,
        )
        if out:
            # absorb small errors in worker delay calculation
            out = max(out, self.enqueued)
        return out

    @property
    def stop(self) -> float:
        """When this span tree finished computing, or current timestamp if it didn't
        finish yet.

        Notes
        -----
        This differs from ``TaskGroup.stop`` when there aren't unfinished tasks; is also
        will never be zero.

        See also
        --------
        enqueued
        start
        done
        distributed.scheduler.TaskGroup.stop
        """
        if not self.done:
            return time()
        out = max(tg.stop for tg in self.traverse_groups())
        # absorb small errors in worker delay calculation
        return max(self.enqueued, out)

    @property
    def states(self) -> dict[TaskStateState, int]:
        """The number of tasks currently in each state in this span tree;
        e.g. ``{"memory": 10, "processing": 3, "released": 4, ...}``.

        See also
        --------
        distributed.scheduler.TaskGroup.states
        """
        return sum_mappings(tg.states for tg in self.traverse_groups())

    @property
    def done(self) -> bool:
        """Return True if all tasks in this span tree are completed; False otherwise.

        Notes
        -----
        This property may transition from True to False, e.g. when a new sub-span is
        added or when a worker that contained the only replica of a task in memory
        crashes and the task need to be recomputed.

        See also
        --------
        distributed.scheduler.TaskGroup.done
        """
        return all(tg.done for tg in self.traverse_groups())

    @property
    def all_durations(self) -> dict[str, float]:
        """Cumulative duration of all completed actions in this span tree, by action

        See also
        --------
        duration
        distributed.scheduler.TaskGroup.all_durations
        """
        return sum_mappings(tg.all_durations for tg in self.traverse_groups())

    @property
    def duration(self) -> float:
        """The total amount of time spent on all tasks in this span tree

        See also
        --------
        all_durations
        distributed.scheduler.TaskGroup.duration
        """
        return sum(tg.duration for tg in self.traverse_groups())

    @property
    def nbytes_total(self) -> int:
        """The total number of bytes that this span tree has produced

        See also
        --------
        distributed.scheduler.TaskGroup.nbytes_total
        """
        return sum(tg.nbytes_total for tg in self.traverse_groups())

    @property
    def code(self) -> list[tuple[SourceCode, ...]]:
        """Code snippets, sent by the client on compute(), persist(), and submit().

        Only populated if ``distributed.diagnostics.computations.nframes`` is non-zero.
        """
        # Deduplicate, but preserve order
        return list(
            dict.fromkeys(sc for child in self.traverse_spans() for sc in child._code)
        )

    @property
    def cumulative_worker_metrics(self) -> dict[tuple[Hashable, ...], float]:
        """Replica of Worker.digests_total and Scheduler.cumulative_worker_metrics, but
        only for the metrics that can be attributed to the current span tree.
        The span_id has been removed from the key.

        At the moment of writing, all keys are
        ``("execute", <task prefix>, <activity>, <unit>)``
        but more may be added in the future with a different format; please test for
        ``k[0] == "execute"``.
        """
        out = sum_mappings(
            child._cumulative_worker_metrics for child in self.traverse_spans()
        )
        known_seconds = sum(
            v for k, v in out.items() if k[0] == "execute" and k[-1] == "seconds"
        )
        # Besides rounding errors, you may get negative unknown seconds if a user
        # manually invokes `context_meter.digest_metric`.
        unknown_seconds = max(0.0, self.active_cpu_seconds - known_seconds)

        out["execute", "N/A", "idle or other spans", "seconds"] = unknown_seconds
        return out

    @staticmethod
    def merge(*items: Span) -> Span:
        """Merge multiple spans into a synthetic one.
        The input spans must not be related with each other.
        """
        if not items:
            raise ValueError("Nothing to merge")
        out = Span(
            name=("(merged)",),
            id_="(merged)",
            parent=None,
            total_nthreads_history=items[0]._total_nthreads_history,
        )
        out._total_nthreads_offset = min(
            child._total_nthreads_offset for child in items
        )
        out.children.extend(items)
        out.enqueued = min(child.enqueued for child in items)
        return out

    def _nthreads_timeseries(self) -> Iterator[tuple[float, int]]:
        """Yield (timestamp, number of threads across the cluster), forward-fill"""
        stop = self.stop if self.done else 0
        for t, n in islice(
            self._total_nthreads_history, self._total_nthreads_offset, None
        ):
            if stop and t >= stop:
                break
            yield max(self.enqueued, t), n

    def _active_timeseries(self) -> Iterator[tuple[float, bool]]:
        """If this span is the output of :meth:`merge`, yield
        (timestamp, True if at least one input span is active), forward-fill.
        """
        now = time()
        if self.id != "(merged)":
            yield self.enqueued, True
            yield self.stop if self.done else now, False
            return

        events = []
        for child in self.children:
            events.append((child.enqueued, 1))
            events.append((child.stop if child.done else now, -1))
        events.sort()

        n_active = 0
        for t, delta in events:
            if not n_active:
                assert delta > 0
                yield t, True
            n_active += delta
            if n_active == 0:
                yield t, False

    @property
    def nthreads_intervals(self) -> list[tuple[float, float, int]]:
        """
        Returns
        ------
        List of tuples:

        - begin timestamp
        - end timestamp
        - Scheduler.total_nthreads during this interval

        When the Span is the output of :meth:`merge`, the intervals may not be
        contiguous.

        See Also
        --------
        enqueued
        stop
        active_cpu_seconds
        distributed.scheduler.SchedulerState.total_nthreads
        """
        nthreads_t, nthreads_count = zip(*self._nthreads_timeseries())
        is_active_t, is_active_flag = zip(*self._active_timeseries())
        t_interp = sorted({*nthreads_t, *is_active_t})
        nthreads_count_interp = ffill(t_interp, nthreads_t, nthreads_count, left=0)
        is_active_flag_interp = ffill(t_interp, is_active_t, is_active_flag, left=False)
        return [
            (t0, t1, n)
            for t0, t1, n, active in zip(
                t_interp, t_interp[1:], nthreads_count_interp, is_active_flag_interp
            )
            if active
        ]

    @property
    def active_cpu_seconds(self) -> float:
        """Return number of CPU seconds that were made available on the cluster while
        this Span was running; in other words
        ``(Span.stop - Span.enqueued) * Scheduler.total_nthreads``.

        This accounts for workers joining and leaving the cluster while this Span was
        active. If this Span is the output of :meth:`merge`, do not count gaps between
        input spans.

        See Also
        --------
        enqueued
        stop
        nthreads_intervals
        distributed.scheduler.SchedulerState.total_nthreads
        """
        return sum((t1 - t0) * nthreads for t0, t1, nthreads in self.nthreads_intervals)


class SpansSchedulerExtension:
    """Scheduler extension for spans support"""

    scheduler: Scheduler

    #: All Span objects by id
    spans: dict[str, Span]

    #: Only the spans that don't have any parents, sorted by creation time.
    #: This is a convenience helper structure to speed up searches.
    root_spans: list[Span]

    #: All spans, keyed by their full name and sorted by creation time.
    #: This is a convenience helper structure to speed up searches.
    spans_search_by_name: defaultdict[tuple[str, ...], list[Span]]

    #: All spans, keyed by the individual tags that make up their name and sorted by
    #: creation time.
    #: This is a convenience helper structure to speed up searches.
    #:
    #: See Also
    #: --------
    #: find_by_tags
    #: merge_by_tags
    spans_search_by_tag: defaultdict[str, list[Span]]

    def __init__(self, scheduler: Scheduler):
        self.scheduler = scheduler
        self.spans = {}
        self.root_spans = []
        self.spans_search_by_name = defaultdict(list)
        self.spans_search_by_tag = defaultdict(list)

    def observe_tasks(
        self, tss: Iterable[TaskState], code: tuple[SourceCode, ...]
    ) -> None:
        """Acknowledge the existence of runnable tasks on the scheduler. These may
        either be new tasks, tasks that were previously unrunnable, or tasks that were
        already fed into this method already.

        Attach newly observed tasks to either the desired span or to ("default", ).
        Update TaskGroup.span_id and wipe TaskState.annotations["span"].
        """
        default_span = None

        for ts in tss:
            # You may have different tasks belonging to the same TaskGroup but to
            # different spans. If that happens, arbitrarily force everything onto the
            # span of the earliest encountered TaskGroup.
            tg = ts.group
            if tg.span_id:
                span = self.spans[tg.span_id]
            else:
                ann = ts.annotations.get("span")
                if ann:
                    span = self._ensure_span(ann["name"], ann["ids"])
                else:
                    if not default_span:
                        default_span = self._ensure_default_span()
                    span = default_span

                tg.span_id = span.id
                span.groups.add(tg)

            if code:
                span._code[code] = None

            # The span may be completely different from the one referenced by the
            # annotation, due to the TaskGroup collision issue explained above.
            # Remove the annotation to avoid confusion, and instead rely on
            # distributed.scheduler.TaskState.group.span_id and
            # distributed.worker_state_machine.TaskState.span_id.
            ts.annotations.pop("span", None)

    def _ensure_default_span(self) -> Span:
        """Return the currently active default span, or create one if the previous one
        terminated. In other words, do not reuse the previous default span if all tasks
        that were not explicitly annotated with :func:`spans` on the client side are
        finished.
        """
        defaults = self.spans_search_by_name["default",]
        if defaults and not defaults[-1].done:
            return defaults[-1]
        return self._ensure_span(("default",), (str(uuid.uuid4()),))

    def _ensure_span(self, name: tuple[str, ...], ids: tuple[str, ...]) -> Span:
        """Create Span if it doesn't exist and return it"""
        try:
            return self.spans[ids[-1]]
        except KeyError:
            pass

        assert len(name) == len(ids)
        assert len(name) > 0

        parent = None
        for i in range(1, len(name)):
            parent = self._ensure_span(name[:i], ids[:i])

        span = Span(
            name=name,
            id_=ids[-1],
            parent=parent,
            total_nthreads_history=self.scheduler.total_nthreads_history,
        )
        self.spans[span.id] = span
        self.spans_search_by_name[name].append(span)
        for tag in name:
            self.spans_search_by_tag[tag].append(span)
        if parent:
            parent.children.append(span)
        else:
            self.root_spans.append(span)

        return span

    def find_by_tags(self, *tags: str) -> Iterator[Span]:
        """Yield all spans that contain any of the given tags.
        When a tag is shared both by a span and its (grand)children, only return the
        parent.
        """
        by_level = defaultdict(list)
        for tag in tags:
            for sp in self.spans_search_by_tag[tag]:
                by_level[len(sp.name)].append(sp)

        seen = set()
        for _, level in sorted(by_level.items()):
            seen.update(level)
            for sp in level:
                if sp.parent not in seen:
                    yield sp

    def merge_all(self) -> Span:
        """Return a synthetic Span which is the sum of all spans"""
        return Span.merge(*self.root_spans)

    def merge_by_tags(self, *tags: str) -> Span:
        """Return a synthetic Span which is the sum of all spans containing the given
        tags
        """
        return Span.merge(*self.find_by_tags(*tags))

    def heartbeat(
        self, ws: WorkerState, data: dict[tuple[Hashable, ...], float]
    ) -> None:
        """Triggered by SpansWorkerExtension.heartbeat().

        Populate :meth:`Span.cumulative_worker_metrics` with data from the worker.

        See also
        --------
        SpansWorkerExtension.heartbeat
        Span.cumulative_worker_metrics
        """
        for (context, span_id, *other), v in data.items():
            assert isinstance(span_id, str)
            span = self.spans[span_id]
            span._cumulative_worker_metrics[(context, *other)] += v


class SpansWorkerExtension:
    """Worker extension for spans support"""

    worker: Worker
    digests_total_since_heartbeat: dict[tuple[Hashable, ...], float]

    def __init__(self, worker: Worker):
        self.worker = worker
        self.digests_total_since_heartbeat = {}

    def collect_digests(self) -> None:
        """Make a local copy of Worker.digests_total_since_heartbeat. We can't just
        parse it directly in heartbeat() as the event loop may be yielded between its
        call and `self.worker.digests_total_since_heartbeat.clear()`, causing the
        scheduler to become misaligned with the workers.
        """
        # Note: this method may be called spuriously by Worker._register_with_scheduler,
        # but when it does it's guaranteed not to find any metrics
        assert not self.digests_total_since_heartbeat
        self.digests_total_since_heartbeat = {
            k: v
            for k, v in self.worker.digests_total_since_heartbeat.items()
            if isinstance(k, tuple) and k[0] == "execute"
        }

    def heartbeat(self) -> dict[tuple[Hashable, ...], float]:
        """Apportion the metrics that do have a span to the Spans on the scheduler

        Returns
        -------
        ``{(context, span_id, prefix, activity, unit): value}}``

        See also
        --------
        SpansSchedulerExtension.heartbeat
        Span.cumulative_worker_metrics
        distributed.worker.Worker.get_metrics
        """
        out = self.digests_total_since_heartbeat
        self.digests_total_since_heartbeat = {}
        return out
