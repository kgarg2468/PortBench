"""Taxonomy, pricing and model naming — the editable tables the aggregator reads.

Everything in here is a *policy* decision (how a rustc code is filed, what a token costs,
what a model is called on the page). `aggregate.py` holds only mechanics, so a taxonomy or
pricing revision is a one-file diff that never touches the counting logic.

Three tables:

  BUCKETS        rustc error code -> failure bucket, plus the bucket metadata the site renders
  CODE_MESSAGES  rustc error code -> one-line human gloss, for the "top codes" list
  PRICING        model -> per-MTok USD list price (see the honesty note on PRICING_BASIS)

The bucket ids are load-bearing: `site/js/app.js` keys its colour map off exactly
`borrow`, `types`, `imports`, `other_compile`, `test_fail`, `harness`. Renaming one here
without renaming it there silently greys out a bar.
"""

from __future__ import annotations

# --------------------------------------------------------------------------- verdicts

PASS = "PASS"
COMPILE_ERROR = "COMPILE_ERROR"
TEST_FAIL = "TEST_FAIL"
SUITE_ERROR = "SUITE_ERROR"
EXTRACTION_ERROR = "EXTRACTION_ERROR"
TRANSPORT_ERROR = "TRANSPORT_ERROR"
TIMEOUT = "TIMEOUT"

# Render order for the stacked verdict-mix bar. Anything the harness emits that is missing
# from this list is appended alphabetically by the aggregator, so a new verdict never vanishes.
VERDICT_ORDER = [
    PASS,
    COMPILE_ERROR,
    TEST_FAIL,
    SUITE_ERROR,
    EXTRACTION_ERROR,
    TRANSPORT_ERROR,
    TIMEOUT,
]

# Verdicts that mean "the model call never succeeded, so there is no output to judge".
# `aggregate.py` promotes these to `no-data` only when they land on attempt 0 *and* no
# attempt 1 exists.
#
# TIMEOUT is deliberately NOT here. In `harness/score.py` a TIMEOUT verdict is what the
# *test run* returns when it blows `--test-timeout`: the model produced code, the code was
# injected, and the suite then hung or ran forever. That is a genuine failure of the port —
# it keeps its place in the denominator, its tokens count towards cost, and it is bucketed
# in the taxonomy like any other failure.
#
# Caveat worth knowing: `harness/models.py` also raises `TransportError(timed_out=True)` when
# the *model CLI itself* times out, and `harness/run.py` writes that as TIMEOUT too. Those
# records carry null tokens and a populated `note`, so the two sources are distinguishable in
# principle, but one verdict string covers both. Splitting them (MODEL_TIMEOUT vs
# TEST_TIMEOUT) is a harness change; until then the test-run reading wins, because it is the
# one that has actually occurred in every sweep on disk.
NO_DATA_VERDICTS = (TRANSPORT_ERROR,)

# Verdicts the harness re-prompts on, copied from `harness/run.py: REPAIR_ELIGIBLE`. Used to
# tell "the task ended here, by design" apart from "the process died before the repair round"
# — the latter is an incomplete task, not a model result, and is flagged as `repair_missing`.
REPAIR_ELIGIBLE = (COMPILE_ERROR, TEST_FAIL, SUITE_ERROR)


# --------------------------------------------------------------------------- taxonomy

# Bucket order is display order, top to bottom in the legend and left to right in the bars.
#
# Multi-code attempts: the harness stores `error_class_hint` as a *sorted, de-duplicated*
# list (see `harness/score.py: error_codes`). We bucket by the FIRST element of that list.
# Because the list is sorted, "first" means "numerically lowest code present", which is a
# stable, reproducible choice — not a severity ranking. Two runs that hit the same set of
# codes always land in the same bucket, which is the property that matters for a leaderboard.
# The alternative (bucket by "most severe") needs a severity table nobody can defend, and the
# alternative of counting an attempt once per bucket breaks the invariant that the buckets
# for an attempt sum to that attempt's failure count.

BUCKETS = [
    {
        "id": "borrow",
        "label": "Borrow checker & lifetimes",
        "short": "Borrow / lifetimes",
        "blurb": "Ownership, moves, aliasing, dangling references. The part of Rust that has "
                 "no Python analogue, so there is nothing in the source to translate.",
        "codes": [
            "E0106",  # missing lifetime specifier
            "E0261",  # use of undeclared lifetime name
            "E0262",  # invalid lifetime parameter name ('static)
            "E0309",  # parameter type may not live long enough
            "E0310",  # parameter type may not live long enough ('static bound needed)
            "E0311",  # returned value does not live long enough
            "E0312",  # lifetime of reference outlives lifetime of borrowed content
            "E0373",  # closure may outlive the current function
            "E0381",  # used binding is possibly-uninitialized
            "E0382",  # borrow/use of moved value  (the canonical Python-to-Rust faceplant)
            "E0383",  # partial reinitialization of an uninitialized structure
            "E0384",  # cannot assign twice to immutable variable
            "E0499",  # cannot borrow as mutable more than once at a time
            "E0500",  # closure requires unique access but value is already borrowed
            "E0501",  # cannot borrow because previously borrowed by a closure
            "E0502",  # cannot borrow as mutable because also borrowed as immutable
            "E0503",  # cannot use value because it was mutably borrowed
            "E0504",  # cannot move into closure because it is borrowed
            "E0505",  # cannot move out of value because it is borrowed
            "E0506",  # cannot assign to a borrowed value
            "E0507",  # cannot move out of borrowed content
            "E0508",  # cannot move out of an array / index expression
            "E0509",  # cannot move out of a type implementing Drop
            "E0510",  # cannot mutate the match scrutinee while it is borrowed
            "E0515",  # cannot return a reference to a local variable
            "E0516",  # `typeof` is reserved.  Kept here because the taxonomy spec lists it
                      # under borrow/lifetimes; rustc actually uses it for the reserved
                      # keyword, and it has never fired in this dataset.
            "E0521",  # borrowed data escapes outside of closure
            "E0524",  # two closures require unique access to the same value
            "E0525",  # expected a closure that implements Fn, but it only implements FnOnce
            "E0596",  # cannot borrow as mutable (binding not declared `mut`)
            "E0597",  # borrowed value does not live long enough
            "E0621",  # explicit lifetime required in the type of an argument
            "E0623",  # lifetime mismatch between two references
            "E0700",  # hidden type for `impl Trait` captures lifetime that does not appear
            "E0712",  # thread-local variable borrowed past the end of its scope
            "E0713",  # borrow of a value escapes the function body
            "E0716",  # temporary value dropped while borrowed
            "E0759",  # argument requires that it is borrowed for `'static`
        ],
    },
    {
        "id": "types",
        "label": "Types & traits",
        "short": "Types / traits",
        "blurb": "Mismatched types, unsatisfied trait bounds, methods and fields that do not "
                 "exist on the receiver. Python has one integer type and no trait bounds.",
        "codes": [
            "E0004",  # non-exhaustive patterns in a match
            "E0023",  # wrong number of fields in a tuple-struct pattern
            "E0026",  # struct pattern names a field the struct does not have
            "E0027",  # struct pattern does not mention all fields
            "E0033",  # type cannot be dereferenced (unsized)
            "E0038",  # trait is not dyn-compatible / object safe
            "E0053",  # method has an incompatible type for its trait
            "E0055",  # recursion limit reached while auto-dereferencing
            "E0057",  # closure called with the wrong number of arguments
            "E0060",  # wrong number of arguments to a variadic extern fn
            "E0061",  # wrong number of arguments to a function
            "E0062",  # struct literal specifies a field more than once
            "E0063",  # struct literal is missing fields
            "E0069",  # `return;` in a function with a non-() return type
            "E0070",  # invalid left-hand side of an assignment
            "E0071",  # struct literal syntax used on a non-struct
            "E0107",  # wrong number of generic arguments
            "E0109",  # type arguments not allowed on this item
            "E0119",  # conflicting trait implementations
            "E0121",  # type placeholder `_` not allowed in this position
            "E0191",  # associated type must be specified
            "E0195",  # lifetime parameters do not match the trait declaration
            "E0207",  # unconstrained type parameter in an impl
            "E0210",  # orphan rule: type parameter must be covered by a local type
            "E0220",  # associated type not found on the trait
            "E0223",  # ambiguous associated type
            "E0271",  # type mismatch resolving an associated type in a trait bound
            "E0275",  # overflow evaluating a trait requirement
            "E0276",  # impl has a stricter requirement than the trait
            "E0277",  # the trait bound is not satisfied  (second most common in this sweep)
            "E0281",  # type mismatch on a closure argument
            "E0282",  # type annotations needed
            "E0283",  # type annotations needed: cannot infer type (ambiguous impls)
            "E0284",  # type annotations needed for a return-position associated type
            "E0308",  # mismatched types  (the single most common code in this sweep)
            "E0369",  # binary operation not supported for this type
            "E0370",  # enum discriminant overflowed
            "E0393",  # type parameter of a trait must be specified
            "E0407",  # method is not a member of the trait
            "E0451",  # field of a struct is private (struct-literal construction)
            "E0560",  # struct has no field named X
            "E0599",  # no method named X found for this type
            "E0608",  # cannot index into a value of this type
            "E0609",  # no field X on this type
            "E0610",  # cannot access fields of a primitive / raw pointer
            "E0614",  # type cannot be dereferenced with `*`
            "E0615",  # attempted to take the value of a method (missing call parens)
            "E0616",  # field is private
            "E0618",  # called something that is not a function
            "E0624",  # associated method is private
            "E0631",  # closure argument type mismatch
            "E0689",  # ambiguous numeric type for a method call
        ],
    },
    {
        "id": "imports",
        "label": "Unresolved imports & deps",
        "short": "Imports / deps",
        "blurb": "The model reached for a path, item or crate that is not in this "
                 "workspace's Cargo.toml. Name resolution failed before typeck ran.",
        "codes": [
            "E0405",  # cannot find trait in this scope
            "E0412",  # cannot find type in this scope
            "E0422",  # cannot find struct / struct variant
            "E0423",  # expected value, found module / struct
            "E0425",  # cannot find value in this scope
            "E0428",  # name defined multiple times
            "E0431",  # `self` import only in a use list
            "E0432",  # unresolved import
            "E0433",  # failed to resolve: use of undeclared crate or module
            "E0463",  # can't find crate
            "E0464",  # multiple matching candidate crates
            "E0468",  # imported macro not found
            "E0469",  # imported macro not found in the crate
            "E0531",  # cannot find tuple struct or tuple variant in this scope
            "E0574",  # expected struct / variant / union, found something else
            "E0576",  # cannot find associated item in trait
            "E0577",  # expected module, found something else
            "E0583",  # file not found for module
            "E0603",  # item is private (path to a non-public item)
        ],
    },
    {
        "id": "other_compile",
        "label": "Other compile errors",
        "short": "Other compile",
        "blurb": "Everything else rustc rejected: syntax, unstable features, malformed items, "
                 "and any code not named in the tables above.",
        "codes": [
            "E0072",  # recursive type has infinite size
            "E0080",  # evaluation of a constant expression failed
            "E0133",  # unsafe operation outside an unsafe block
            "E0201",  # duplicate definitions with the same name in an impl
            "E0433_syntax_placeholder_none",  # (no code) — kept empty on purpose; see below
            "E0601",  # `main` function not found
            "E0658",  # use of an unstable library / language feature
        ],
    },
    {
        "id": "test_fail",
        "label": "Test-logic failures",
        "short": "Test logic",
        "blurb": "It compiled. It was wrong. Semantics lost in translation, caught by the "
                 "project's own suite.",
        "codes": [],
    },
    {
        "id": "harness",
        "label": "Extraction / transport / timeout",
        "short": "Harness",
        "blurb": "No usable function in the response, an API error, or the test budget blown. "
                 "Not a statement about the model's Rust.",
        "codes": [],
    },
]

# `other_compile` is the *fallback*, so its explicit list is illustrative only. Strip the
# placeholder so it never shows up in the emitted JSON.
for _b in BUCKETS:
    if _b["id"] == "other_compile":
        _b["codes"] = [c for c in _b["codes"] if not c.endswith("_placeholder_none")]

OTHER_COMPILE = "other_compile"
BUCKET_IDS = [b["id"] for b in BUCKETS]

# code -> bucket id. Built from BUCKETS so the two can never drift.
CODE_TO_BUCKET: dict[str, str] = {}
for _b in BUCKETS:
    for _code in _b["codes"]:
        # First table wins if a code is listed twice; the loop order is BUCKETS order.
        CODE_TO_BUCKET.setdefault(_code, _b["id"])
del _b


def bucket_for_code(code: str) -> str:
    """Bucket a single rustc code. Unlisted codes fall through to `other_compile`."""
    return CODE_TO_BUCKET.get(code, OTHER_COMPILE)


def bucket_for_attempt(verdict: str, codes) -> str:
    """The single bucket a non-PASS attempt is filed under.

    Verdict first, codes second — the verdict is the harness's own classification and is
    authoritative:

      * TEST_FAIL   -> `test_fail`. `harness/score.py` only reaches a TEST_FAIL verdict when
                       the crate compiled, so "TEST_FAIL with a clean compile" is the whole
                       population of that verdict. rustc codes can still appear in the record
                       (scraped out of an unrelated warning stream); they are ignored here.
      * EXTRACTION_ERROR / TRANSPORT_ERROR / TIMEOUT / SUITE_ERROR -> `harness`. A TIMEOUT
                       record can carry codes too; it is still infrastructure, not Rust.
      * COMPILE_ERROR -> keyed off the first code (see the note on BUCKETS). No codes at all
                       (a bare "could not compile", a link error, a syntax error rustc did not
                       number) -> `other_compile`.
    """
    if verdict == TEST_FAIL:
        return "test_fail"
    if verdict in (EXTRACTION_ERROR, TRANSPORT_ERROR, TIMEOUT, SUITE_ERROR):
        return "harness"
    codes = list(codes or [])
    if not codes:
        return OTHER_COMPILE
    return bucket_for_code(codes[0])


# One-line glosses for the "most frequent rustc codes" strip. Missing codes get a generic
# label rather than being dropped, so a code we have never seen still renders.
CODE_MESSAGES = {
    "E0004": "non-exhaustive patterns in match",
    "E0023": "wrong number of fields in pattern",
    "E0026": "struct pattern names a nonexistent field",
    "E0027": "pattern does not mention all fields",
    "E0038": "trait is not dyn-compatible",
    "E0053": "method has an incompatible type for trait",
    "E0061": "wrong number of arguments to function",
    "E0063": "missing fields in struct literal",
    "E0072": "recursive type has infinite size",
    "E0106": "missing lifetime specifier",
    "E0107": "wrong number of generic arguments",
    "E0119": "conflicting trait implementations",
    "E0133": "unsafe operation outside unsafe block",
    "E0191": "associated type must be specified",
    "E0207": "unconstrained type parameter in impl",
    "E0223": "ambiguous associated type",
    "E0271": "type mismatch resolving associated type",
    "E0277": "the trait bound is not satisfied",
    "E0282": "type annotations needed",
    "E0283": "cannot infer type: multiple impls apply",
    "E0308": "mismatched types",
    "E0369": "binary operation not supported for this type",
    "E0373": "closure may outlive the current function",
    "E0381": "used binding is possibly-uninitialized",
    "E0382": "borrow of moved value",
    "E0384": "cannot assign twice to immutable variable",
    "E0405": "cannot find trait in this scope",
    "E0412": "cannot find type in this scope",
    "E0423": "expected value, found module or struct",
    "E0425": "cannot find value in this scope",
    "E0432": "unresolved import",
    "E0433": "failed to resolve: undeclared crate or module",
    "E0451": "field of struct is private",
    "E0463": "can't find crate",
    "E0499": "cannot borrow as mutable more than once at a time",
    "E0502": "cannot borrow as mutable because also borrowed as immutable",
    "E0503": "cannot use value because it was mutably borrowed",
    "E0505": "cannot move out of value because it is borrowed",
    "E0506": "cannot assign to borrowed value",
    "E0507": "cannot move out of borrowed content",
    "E0515": "cannot return value referencing local variable",
    "E0521": "borrowed data escapes outside of closure",
    "E0560": "struct has no field with this name",
    "E0596": "cannot borrow as mutable, binding not declared mut",
    "E0597": "borrowed value does not live long enough",
    "E0599": "no method named this found for the receiver",
    "E0603": "item is private",
    "E0608": "cannot index into a value of this type",
    "E0609": "no field with this name on the type",
    "E0614": "type cannot be dereferenced",
    "E0615": "attempted to take value of method",
    "E0616": "field is private",
    "E0618": "called an expression that is not a function",
    "E0621": "explicit lifetime required in argument type",
    "E0623": "lifetime mismatch between references",
    "E0631": "closure argument type mismatch",
    "E0658": "use of an unstable feature",
    "E0689": "ambiguous numeric type for method call",
    "E0716": "temporary value dropped while borrowed",
    "E0759": "argument requires that it is borrowed for 'static",
}


def message_for_code(code: str) -> str:
    return CODE_MESSAGES.get(code, "rustc " + code)


# --------------------------------------------------------------------------- models

# Harness model key (the `model` field in the JSONL, and the results/ subdirectory name)
# -> how it is presented on the leaderboard, and which pricing row it uses.
#
# `family` must be one of the families site/css/portbench.css styles: `claude`, `gpt`.
MODEL_REGISTRY = {
    "fable":         {"label": "Claude Fable 5",  "family": "claude", "price": "claude-fable"},
    "opus":          {"label": "Claude Opus 5",   "family": "claude", "price": "claude-opus"},
    "sonnet":        {"label": "Claude Sonnet 5", "family": "claude", "price": "claude-sonnet"},
    "haiku":         {"label": "Claude Haiku 4.5", "family": "claude", "price": "claude-haiku"},
    "gpt-5.6-sol":   {"label": "GPT-5.6 Sol",     "family": "gpt",    "price": "gpt-5.6-sol"},
    "gpt-5.6-terra": {"label": "GPT-5.6 Terra",   "family": "gpt",    "price": "gpt-5.6-terra"},
    "gpt-5.6-luna":  {"label": "GPT-5.6 Luna",    "family": "gpt",    "price": "gpt-5.6-luna"},
    "gpt-5.5":       {"label": "GPT-5.5",         "family": "gpt",    "price": "gpt-5.5"},
}

# Display order when several models are present. Anything not listed sorts after these,
# alphabetically, so a newly added model appears without editing this list.
MODEL_ORDER = [
    "fable", "opus", "sonnet", "haiku",
    "gpt-5.6-sol", "gpt-5.6-terra", "gpt-5.6-luna", "gpt-5.5",
]


def model_meta(model_key: str) -> dict:
    """Label / family / pricing row for a model key, with a usable fallback."""
    known = MODEL_REGISTRY.get(model_key)
    if known:
        return dict(known)
    family = "claude" if model_key.startswith(("fable", "opus", "sonnet", "haiku", "claude")) \
        else ("gpt" if model_key.startswith(("gpt", "o1", "o3")) else "other")
    return {"label": model_key, "family": family, "price": model_key}


def model_sort_key(model_key: str) -> tuple:
    if model_key in MODEL_ORDER:
        return (0, MODEL_ORDER.index(model_key), model_key)
    return (1, 0, model_key)


# --------------------------------------------------------------------------- pricing

# LIST PRICE APPROXIMATION — READ THIS BEFORE QUOTING A DOLLAR FIGURE.
#
# USD per million tokens, public pay-as-you-go API list price. These are not the numbers the
# sweep actually cost: every run in this repository was made through a Claude Max and a Codex
# Max subscription, where the marginal cost of a token is $0. The dollar columns exist to
# answer "what would this cost an API customer", which is the comparable number across
# vendors, and nothing else.
#
# `confidence` is honest about how sure we are of each row:
#   "published"  — a price tier that has been stable and public for a long time.
#   "inferred"   — the model's tier is public but the exact figure is our best reading of it.
# Anything marked "inferred" should be checked against the vendor's pricing page before it is
# put in a blog post. Cache-read and cache-write discounts, batch discounts and reasoning-token
# surcharges are all ignored: input is input, output is output.
#
# Edit these constants, re-run the aggregator, and every dollar figure on the site moves.
PRICING = {
    "claude-fable":  {"in": 15.00, "out": 75.00, "confidence": "inferred",
                      "note": "frontier Claude tier; priced as the Opus-class row"},
    "claude-opus":   {"in": 15.00, "out": 75.00, "confidence": "published",
                      "note": "Claude Opus tier"},
    "claude-sonnet": {"in": 3.00,  "out": 15.00, "confidence": "published",
                      "note": "Claude Sonnet tier (<=200K context)"},
    "claude-haiku":  {"in": 1.00,  "out": 5.00,  "confidence": "published",
                      "note": "Claude Haiku 4.5 tier"},
    "gpt-5.6-sol":   {"in": 1.25,  "out": 10.00, "confidence": "inferred",
                      "note": "GPT-5-class flagship tier"},
    "gpt-5.6-terra": {"in": 0.25,  "out": 2.00,  "confidence": "inferred",
                      "note": "GPT-5-class mini tier"},
    "gpt-5.6-luna":  {"in": 0.05,  "out": 0.40,  "confidence": "inferred",
                      "note": "GPT-5-class nano tier"},
    "gpt-5.5":       {"in": 1.25,  "out": 10.00, "confidence": "inferred",
                      "note": "prior-generation flagship tier"},
}

PRICING_BASIS = (
    "Public pay-as-you-go API list price, USD per million tokens, approximate. Rows marked "
    "confidence=inferred are our best reading of the model's published tier, not a quoted "
    "figure. Cache, batch and reasoning-token adjustments are ignored. The sweep itself ran "
    "on Claude Max and Codex Max subscriptions, where the marginal cost of these tokens was "
    "$0 — the dollar columns answer 'what would an API customer pay', nothing more."
)


def price_for(model_key: str):
    """Pricing row for a model key, or None if we have no defensible price."""
    row = PRICING.get(model_meta(model_key)["price"])
    return dict(row) if row else None


def cost_usd(model_key: str, tokens_in, tokens_out):
    """Dollar cost of a token pair, or None.

    None — never 0.0 — when either the price or the token counts are unavailable. An early
    codex smoke run reports no usage at all; charging it $0.00 would put it top of the
    "cheapest per solved task" column, which is exactly backwards.
    """
    row = price_for(model_key)
    if row is None or tokens_in is None or tokens_out is None:
        return None
    return tokens_in / 1e6 * row["in"] + tokens_out / 1e6 * row["out"]
