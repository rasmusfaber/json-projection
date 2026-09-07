"""Deterministic JSON payloads + models for bench3. SHAPES: name -> zero-arg builder returning Shape."""
import json, random
from dataclasses import dataclass
from typing import Any, Callable
from pydantic import BaseModel

NAN = float("nan")


@dataclass
class Shape:
    name: str
    raw: bytes
    model: type[BaseModel]          # validated by `naive` (full third-party model, or thin model for unknown-key shapes)
    exclude: dict                   # {Cls: {field,...}} for excluding(); {} = nothing to exclude
    helper_na: bool                 # True: no known field to exclude -> helper == naive
    spec: dict                      # jsonproject.project_paths spec
    kept: Callable[[Any], Any]      # instance -> JSON-able value holding only the kept fields
    note: str = ""


def dumps(o):  # compact, deterministic, NaN allowed
    return json.dumps(o, separators=(",", ":")).encode()


class Thin(BaseModel):
    a: float


class Ten(BaseModel):
    f0: float; f1: float; f2: float; f3: float; f4: float
    f5: float; f6: float; f7: float; f8: float; f9: float


TEN_KEYS = [f"f{i}" for i in range(10)]
TEN_SPEC = {k: True for k in TEN_KEYS}


def ten_vals(rng):
    d = {k: rng.random() * 100 for k in TEN_KEYS}
    d["f3"] = NAN
    return d


def kept_ten(m):
    return {k: getattr(m, k) for k in TEN_KEYS}


class Event(BaseModel):
    ts: float
    payload: dict[str, Any]


class Run(BaseModel):
    name: str
    events: list[Event]


class Sample(BaseModel):
    id: int
    score: float
    events: list[Event]


class Log(BaseModel):
    name: str
    samples: list[Sample]


class Sub(BaseModel):
    x: float
    y: str


class Big(BaseModel):
    ints: list[int]
    strs: list[str]
    nested: list[Sub]


class Item(BaseModel):
    id: int
    v: float


class Half(BaseModel):
    keep: list[Item]


class Pusher(BaseModel):
    name: str


class Sender(BaseModel):
    login: str


class Push(BaseModel):
    ref: str
    before: str
    after: str
    pusher: Pusher
    sender: Sender


class Meta(BaseModel):
    name: str
    namespace: str
    labels: dict[str, str]
    annotations: dict[str, str]


class Pod(BaseModel):
    metadata: Meta
    spec: dict
    status: dict


class PodList(BaseModel):
    items: list[Pod]


def event(j):
    return {"ts": j, "payload": {"k": "v" * 20}}


def s1():
    raw = b'{"a":1.5,"skipme":[' + b",".join([b'{"k":"v"}'] * 50000) + b"]}"
    return Shape("S1_root_unknown_array", raw, Thin, {}, True, {"a": True}, lambda m: {"a": m.a})


def s2():
    raw = dumps({"name": "run", "events": [event(j) for j in range(25000)]})
    return Shape("S2_root_known_excluded", raw, Run, {Run: {"events"}}, False, {"name": True}, lambda m: {"name": m.name})


def s3():
    samples = [{"id": i, "score": NAN if i % 7 == 0 else i / 10, "events": [event(j) for j in range(50)]} for i in range(500)]
    raw = dumps({"name": "run", "samples": samples})
    spec = {"name": True, "samples": {"__all__": {"id": True, "score": True}}}
    return Shape("S3_nested_excluded", raw, Log, {Sample: {"events"}}, False, spec,
                 lambda m: {"name": m.name, "samples": [[s.id, s.score] for s in m.samples]})


def s4():
    rng = random.Random(4)
    d = ten_vals(rng)
    d.update({f"u{i}": {"x": 1} for i in range(20000)})
    return Shape("S4_many_small_unknown", dumps(d), Ten, {}, True, TEN_SPEC, kept_ten)


def s5():
    raw = b'{"a":1.5,"s":"' + b"x" * 1_000_000 + b'"}'
    return Shape("S5_big_string_unknown", raw, Thin, {}, True, {"a": True}, lambda m: {"a": m.a})


def s6():
    raw = dumps(ten_vals(random.Random(6)))
    return Shape("S6_all_known_small", raw, Ten, {}, False, TEN_SPEC, kept_ten, "nothing to skip; empty exclude set")


def s7():
    rng = random.Random(7)
    d = {"ints": [rng.randrange(10**9) for _ in range(30000)],
         "strs": [f"string-number-{i:06d}" for i in range(25000)],
         "nested": [{"x": NAN if i % 11 == 0 else rng.random(), "y": f"label{i}"} for i in range(12000)]}
    spec = {"ints": True, "strs": True, "nested": {"__all__": {"x": True, "y": True}}}
    return Shape("S7_all_known_large", dumps(d), Big, {}, False, spec,
                 lambda m: {"ints": m.ints, "strs": m.strs, "nested": [[s.x, s.y] for s in m.nested]},
                 "nothing to skip; empty exclude set; projector copies everything")


def s8():
    rng = random.Random(8)
    keep = [{"id": i, "v": NAN if i % 13 == 0 else rng.random()} for i in range(16000)]   # ~500 KB
    junk = [{"id": i, "name": f"junk-{i}", "tags": ["a", "b", "c"]} for i in range(9000)]  # ~500 KB
    raw = dumps({"keep": keep, "junk": junk})
    return Shape("S8_half_half", raw, Half, {}, True, {"keep": True}, lambda m: {"keep": [[i.id, i.v] for i in m.keep]})


def s9():
    one = b"[" * 60 + b"1" + b"]" * 60
    raw = b'{"a":1.5,"deep":[' + b",".join([one] * 5000) + b"]}"
    return Shape("S9_deep_unknown", raw, Thin, {}, True, {"a": True}, lambda m: {"a": m.a})


def s10():
    rng = random.Random(10)
    keys = [f"f{i}" for i in range(10)] + [f"u{i}" for i in range(990)]
    rng.shuffle(keys)
    ten = ten_vals(rng)
    d = {k: (ten[k] if k in ten else [1, 2, 3]) for k in keys}
    return Shape("S10_wide_root", dumps(d), Ten, {}, True, TEN_SPEC, kept_ten)


def s11():
    rng = random.Random(11)
    sha = lambda: "".join(rng.choice("0123456789abcdef") for _ in range(40))
    user = lambda login: {"login": login, "id": rng.randrange(10**7), "node_id": "MDQ6VXNlcjE=", "avatar_url": f"https://avatars.githubusercontent.com/u/{rng.randrange(10**7)}?v=4",
                          "gravatar_id": "", "url": f"https://api.github.com/users/{login}", "html_url": f"https://github.com/{login}",
                          "followers_url": f"https://api.github.com/users/{login}/followers", "type": "User", "site_admin": False}
    def commit():
        return {"id": sha(), "tree_id": sha(), "distinct": True, "message": "Fix the thing\n\n" + "Lorem ipsum dolor sit amet, consectetur adipiscing elit. " * 3,
                "timestamp": "2026-09-06T12:00:00+02:00", "url": f"https://github.com/octo/repo/commit/{sha()}",
                "author": {"name": "Octo Cat", "email": "octo@example.com", "username": "octocat"},
                "committer": {"name": "GitHub", "email": "noreply@github.com", "username": "web-flow"},
                "added": [f"src/new/file_{rng.randrange(1000)}.py" for _ in range(3)],
                "removed": [f"src/old/file_{rng.randrange(1000)}.py" for _ in range(2)],
                "modified": [f"src/mod/file_{rng.randrange(1000)}.py" for _ in range(6)]}
    repo = {"id": 1296269, "node_id": "MDEwOlJlcG9zaXRvcnkxMjk2MjY5", "name": "repo", "full_name": "octo/repo", "private": False, "owner": user("octo"),
            "description": "A repository", "fork": False, "created_at": 1700000000, "updated_at": "2026-09-06T00:00:00Z", "pushed_at": 1750000000,
            "default_branch": "main", "master_branch": "main", "stargazers_count": 80, "forks_count": 9, "open_issues_count": 0, "license": None}
    for k in ["url", "forks_url", "keys_url", "collaborators_url", "teams_url", "hooks_url", "issue_events_url", "events_url", "assignees_url", "branches_url",
              "tags_url", "blobs_url", "git_tags_url", "git_refs_url", "trees_url", "statuses_url", "languages_url", "stargazers_url", "contributors_url",
              "subscribers_url", "subscription_url", "commits_url", "git_commits_url", "comments_url", "issue_comment_url", "contents_url", "compare_url",
              "merges_url", "archive_url", "downloads_url", "issues_url", "pulls_url", "milestones_url", "notifications_url", "labels_url", "releases_url", "deployments_url"]:
        repo[k] = f"https://api.github.com/repos/octo/repo/{k[:-4]}{{/id}}"
    repo["topics"] = [f"topic-{i}" for i in range(6000)]                                   # padding -> ~150 KB
    repo["custom_properties"] = {f"prop{i}": f"value-{i}" for i in range(3000)}
    commits = [commit() for _ in range(200)]
    d = {"ref": "refs/heads/main", "before": sha(), "after": sha(), "created": False, "deleted": False, "forced": False, "base_ref": None,
         "compare": "https://github.com/octo/repo/compare/abc...def", "commits": commits, "head_commit": commits[-1], "repository": repo,
         "pusher": {"name": "octocat", "email": "octo@example.com"}, "sender": user("octocat"),
         "installation": {"id": 123, "node_id": "MDIzOkluc3RhbGxhdGlvbjEyMw=="}, "organization": user("octo-org"),
         "enterprise": {"id": 1, "slug": "octo", "name": "Octo Enterprise", "avatar_url": "https://example.com/e.png"}}
    for i in range(14):  # bring root key count to ~30
        d[f"x_meta_{i}"] = {"n": i, "flag": bool(i % 2)} if i % 2 else f"meta-{i}"
    spec = {"ref": True, "before": True, "after": True, "pusher": {"name": True}, "sender": {"login": True}}
    return Shape("S11_webhook_like", dumps(d), Push, {}, True, spec,
                 lambda m: {"ref": m.ref, "before": m.before, "after": m.after, "pusher": m.pusher.name, "sender": m.sender.login})


def s12():
    rng = random.Random(12)
    def pod(i):
        return {"metadata": {"name": f"pod-{i:04d}-{sha8(rng)}", "namespace": rng.choice(["default", "kube-system", "prod", "stg"]),
                             "labels": {f"app.kubernetes.io/{k}": f"v{rng.randrange(100)}" for k in ["name", "instance", "version", "component", "part-of", "managed-by"]},
                             "annotations": {f"example.com/annotation-{k}": "x" * 40 for k in range(20)}},   # ~1 KB
                "spec": {"containers": [{"name": f"c{c}", "image": f"registry.example.com/img{c}:1.{rng.randrange(50)}",
                                         "ports": [{"containerPort": 8000 + p, "protocol": "TCP"} for p in range(3)],
                                         "env": [{"name": f"ENV_{e}", "value": f"val-{e}"} for e in range(8)],
                                         "resources": {"limits": {"cpu": "500m", "memory": "512Mi"}, "requests": {"cpu": "250m", "memory": "256Mi"}}} for c in range(3)],
                         "nodeName": f"node-{rng.randrange(50)}", "restartPolicy": "Always", "dnsPolicy": "ClusterFirst", "serviceAccountName": "default",
                         "tolerations": [{"key": f"k{t}", "operator": "Exists", "effect": "NoSchedule"} for t in range(3)]},   # ~1.5 KB
                "status": {"phase": "Running", "podIP": f"10.0.{rng.randrange(255)}.{rng.randrange(255)}", "hostIP": f"192.168.{rng.randrange(255)}.{rng.randrange(255)}",
                           "startTime": "2026-09-06T00:00:00Z",
                           "conditions": [{"type": t, "status": "True", "lastTransitionTime": "2026-09-06T00:00:00Z"} for t in ["Initialized", "Ready", "ContainersReady", "PodScheduled"]],
                           "containerStatuses": [{"name": f"c{c}", "ready": True, "restartCount": rng.randrange(3), "image": f"registry.example.com/img{c}:1.0",
                                                  "imageID": "sha256:" + "ab" * 32, "containerID": "containerd://" + "cd" * 32,
                                                  "state": {"running": {"startedAt": "2026-09-06T00:00:00Z"}}} for c in range(3)]}}   # ~1 KB
    raw = dumps({"apiVersion": "v1", "kind": "List", "items": [pod(i) for i in range(500)]})
    spec = {"items": {"__all__": {"metadata": {"name": True, "namespace": True}}}}
    return Shape("S12_k8s_list_like", raw, PodList, {Pod: {"spec", "status"}, Meta: {"labels", "annotations"}}, False, spec,
                 lambda m: {"items": [[p.metadata.name, p.metadata.namespace] for p in m.items]})


def sha8(rng):
    return "".join(rng.choice("0123456789abcdef") for _ in range(8))


SHAPES = {f.__name__.upper(): f for f in [s1, s2, s3, s4, s5, s6, s7, s8, s9, s10, s11, s12]}


def count_leaves(o):
    if isinstance(o, dict): return sum(count_leaves(v) for v in o.values())
    if isinstance(o, list): return sum(count_leaves(v) for v in o)
    return 1


if __name__ == "__main__":
    for k, f in SHAPES.items():
        sh = f()
        print(f"{k:4} {sh.name:24} {len(sh.raw)/1e6:6.3f} MB  model={sh.model.__name__:8} exclude={ {c.__name__: sorted(v) for c, v in sh.exclude.items()} } helper_na={sh.helper_na}")
