# ruff: noqa: E501, E702
# Cost model: per active user per month. All token counts are estimates (see report).
DAYS, WEEKS = 22, 4.33
PEAK_SHARE = 0.4  # share of working-hour calls in DeepSeek peak (06-10 UTC) for MSK 9-19 users
vol = dict(classify=10, filter=23.1, rank=3.5, deadlines=2.3, commit=27, compose=23.8)  # per day
pipelines = 23.1
regen_share = 2.1 / 23.1
retry_share = 0.03
SYS = dict(classify=850, filter=760, rank=760, deadlines=640, commit=960, compose=2810, profile=500)
SCHEMA = dict(
    classify=380, filter=330, rank=330, deadlines=360, commit=300, compose=1200, profile=540
)
DATA = dict(
    classify=630,
    filter=6400,
    rank=1360,
    deadlines=1000,
    commit=420,
    compose_profile=900,
    compose_rest=1500,
)
VIS = dict(classify=120, filter=435, rank=300, deadlines=150, commit=120, compose=400, profile=1500)
TH_ANTH = dict(
    classify=100, filter=200, rank=150, deadlines=150, commit=100, compose=1000, profile=4000
)
TH_DS_DEFAULT = dict(
    classify=800, filter=1000, rank=800, deadlines=800, commit=800, compose=1800, profile=6000
)
TH_DS_TUNED = dict(
    classify=0, filter=0, rank=0, deadlines=150, commit=100, compose=900, profile=4000
)
PROFILE_IN = dict(glob=20000, contact=7500)
PROFILES_WEEK = dict(glob=1, contact=3)

A = {
    "haiku": (0.10, 0.01, 0.125, 0.50),
    "opus": (4.0, 0.20, 5.0, 20.0),
    "sonnet": (2.0, 0.20, 2.5, 10.0),
    "opus1h": (4.0, 0.20, 8.0, 20.0),
}  # in, read, write5m(or 1h), out per M
D = {"flash": (0.30, 0.006, 1.20), "pro": (1.32, 0.044, 3.96)}  # peak: miss, hit, out
work = PEAK_SHARE + (1 - PEAK_SHARE) * 0.5  # blended price factor during working hours
M = 1e6


def anth(variant, compose_model="opus"):
    out = {}
    for r in ["classify", "filter", "rank", "deadlines", "commit"]:
        pin, pr, pw, po = A["haiku"]
        tin = SYS[r] + DATA[r]
        if variant == "proposed" and r in (
            "filter",
        ):  # cache filter system prompt (>=512) across users
            c = (DATA[r] * pin + SYS[r] * pr) / M
        else:
            c = tin * pin / M
        c += (VIS[r] + TH_ANTH[r]) * po / M
        out[r] = c * vol[r] * DAYS
    pin, pr, pw, po = A[compose_model]
    pre = SYS["compose"] + DATA["compose_profile"]
    if variant == "current":  # one breakpoint after profile, 5 min TTL, ~30% hits
        h = 0.30
        cin = h * pre * pr + (1 - h) * pre * pw + DATA["compose_rest"] * pin
    else:  # breakpoint after system (shared, ~98% hit) + after profile, both 1h (~80% hit)
        w1h = pin * 2.0
        cin = (
            SYS["compose"] * (0.98 * pr + 0.02 * w1h)
            + DATA["compose_profile"] * (0.8 * pr + 0.2 * w1h)
            + DATA["compose_rest"] * pin
        )
    c = cin / M + (VIS["compose"] + TH_ANTH["compose"]) * po / M
    out["compose"] = c * vol["compose"] * DAYS
    pin, pr, pw, po = A["opus"]
    p = 0
    for k in ("glob", "contact"):
        p += (
            PROFILES_WEEK[k]
            * ((PROFILE_IN[k] + SYS["profile"]) * pin + (VIS["profile"] + TH_ANTH["profile"]) * po)
            / M
        )
    out["profile"] = p * 0.5 * WEEKS  # Message Batches 50%
    return out


def ds(variant, compose_model="pro"):
    th = TH_DS_TUNED if variant == "tuned" else TH_DS_DEFAULT
    out = {}

    def cost(model, tin_hit, tin_miss, tout, factor):
        miss, hit, o = D[model]
        return (tin_hit * hit + tin_miss * miss + tout * o) * factor / M

    for r in ["classify", "filter", "rank", "deadlines", "commit"]:
        sysx = SYS[r] + SCHEMA[r]
        data = DATA[r]
        if variant == "nocache":
            hit, miss = 0, sysx + data
        else:
            hit, miss = sysx, data
            if r == "filter":
                regen_hit = regen_share * data  # regenerate: identical request
                if variant in ("ordered", "tuned"):
                    reuse = 0.5 * (data - 80)  # anchored history/tasks before incoming
                    hit += reuse + regen_hit
                    miss -= reuse + regen_hit
                else:
                    hit += regen_hit
                    miss -= regen_hit
        out[r] = cost("flash", hit, miss, VIS[r] + th[r], work) * vol[r] * DAYS
    sysx = SYS["compose"] + SCHEMA["compose"]
    prof = DATA["compose_profile"]
    rest = DATA["compose_rest"]
    if variant == "nocache":
        hit, miss = 0, sysx + prof + rest
    else:
        same = regen_share + retry_share  # identical re-sends
        hit = sysx + 0.9 * prof + same * rest
        if variant in ("ordered", "tuned"):
            hit += 0.10 * rest * (1 - same)  # per-chat static fields moved up
        miss = sysx + prof + rest - hit
    out["compose"] = (
        cost(compose_model, hit, miss, VIS["compose"] + th["compose"], work) * vol["compose"] * DAYS
    )
    p = 0
    for k in ("glob", "contact"):
        p += PROFILES_WEEK[k] * cost(
            "pro",
            SYS["profile"] + SCHEMA["profile"] if variant != "nocache" else 0,
            PROFILE_IN[k] + (0 if variant != "nocache" else SYS["profile"] + SCHEMA["profile"]),
            VIS["profile"] + th["profile"],
            0.5,
        )  # Sunday = off-peak
    out["profile"] = p * WEEKS
    return out


def show(name, d):
    tot = sum(d.values())
    drafts = pipelines * DAYS
    print(
        f"{name:42s} total ${tot:7.3f}/user/mo  per draft ${tot / drafts:.4f}  "
        + "  ".join(f"{k}={v:.3f}" for k, v in d.items())
    )
    return tot


r = {}
r["a"] = show("(a) Anthropic current (Opus quality)", anth("current"))
r["a2"] = show("(a') Anthropic + system/1h breakpoints", anth("proposed"))
r["as"] = show("(a-econ) Anthropic current, economy Sonnet", anth("current", "sonnet"))
r["b"] = show("(b) DeepSeek, no cache hits", ds("nocache"))
r["b2"] = show("(b2) DeepSeek, current order, auto cache", ds("current"))
r["c"] = show("(c) DeepSeek, proposed order", ds("ordered"))
r["d"] = show("(d) DeepSeek, proposed order + thinking tuned", ds("tuned"))
r["df"] = show("(d-flash) as (d) but compose on flash", ds("tuned", "flash"))
