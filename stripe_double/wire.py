"""Closed request/projection subset consumed by the existing adapter."""
from dataclasses import dataclass, replace
import json
import re
from urllib.parse import parse_qsl, urlsplit

API_VERSION = "2026-10-28.endive"
LEGACY_API_VERSION = "2025-02-24.acacia"
SEPTEMBER_ENDIVE_API_VERSION = "2026-09-30.endive"


@dataclass(frozen=True)
class ApiProfile:
    protocol_profile: str
    bootstrap_schema: int
    payment_method_types_field: str
    payment_method_types: frozenset[str]
    default_payment_method: str | None
    require_capture_method: bool
    account_type: str | None
    include_payment_balance_transactions: bool
    supports_accounts_v2: bool


_ENDIVE = ApiProfile(protocol_profile="stripe_standard_us_usd_endive_v1", bootstrap_schema=2,
    payment_method_types_field="allowed_payment_method_types[]", payment_method_types=frozenset({"card","us_bank_account"}),
    default_payment_method="card", require_capture_method=False, account_type="standard",
    include_payment_balance_transactions=True, supports_accounts_v2=True)
API_PROFILES = {
    API_VERSION: _ENDIVE,
    LEGACY_API_VERSION: ApiProfile(protocol_profile="stripe_standard_us_usd_test_v1", bootstrap_schema=1,
        payment_method_types_field="payment_method_types[]", payment_method_types=frozenset({"card"}),
        default_payment_method=None, require_capture_method=True, account_type=None,
        include_payment_balance_transactions=False, supports_accounts_v2=False),
    SEPTEMBER_ENDIVE_API_VERSION: replace(_ENDIVE, protocol_profile="stripe_standard_us_usd_endive_202609_v1"),
}
MAX_BODY = 65536
MAX_OBJECTS = 2048
MAX_EVENTS = 8192
SCENARIOS = frozenset("STRIPE-LOCAL-%02d" % n for n in range(1, 10))
CONTROLLER = {"losses":{"payments":"stripe"}, "fees":{"payer":"account"},
              "requirement_collection":"stripe", "stripe_dashboard":{"type":"full"}}
PURPOSES = {"onboarding":"account_onboarding", "account_settings":"account_management",
            "payout_settings":"payouts", "card_payment":"payment_intent"}
FEATURES = {
    "onboarding":{"disable_stripe_user_authentication":False},
    "account_settings":{"external_account_collection":True,"disable_stripe_user_authentication":False},
    "payout_settings":{"external_account_collection":True,"disable_stripe_user_authentication":False,
                       "edit_payout_schedule":True,"standard_payouts":False,"instant_payouts":False},
}
ACCOUNT_FORM = {"country":"US","default_currency":"usd",
                "capabilities[card_payments][requested]":"true","capabilities[transfers][requested]":"true",
                "controller[losses][payments]":"stripe","controller[fees][payer]":"account",
                "controller[requirement_collection]":"stripe","controller[stripe_dashboard][type]":"full"}
ID = re.compile(r"(?:acct|pi|ch|re|fee|fr)_LocalSim[0-9a-f]{32}\Z")
SAVED_CARD_ID = r"pm_LocalSim[0-9a-f]{32}"
PAYOUT_QUOTE_ID = r"pq_LocalSim[0-9a-f]{32}"
TOKEN = re.compile(r"[\x21-\x7e]{1,255}\Z")


class SimulatorError(Exception):
    """Errors contain only a finite code, never credentials or request bodies."""
    def __init__(self, status=400, code="simulator_invalid"):
        self.status, self.code = status, code
        super().__init__(code)

    def result(self):
        message = "Unsupported local operation" if self.code == "simulator_unsupported" else "Local simulator request refused"
        return Result(self.status, {"error":{"type":"invalid_request_error","code":self.code,"message":message}})


@dataclass(frozen=True)
class Result:
    status: int
    body: dict
    drop: bool = False


def need(condition, status=400, code="simulator_invalid"):
    if not condition:
        raise SimulatorError(status, code)


def dumps(value):
    data = json.dumps(value, sort_keys=True, separators=(",",":"), allow_nan=False).encode("utf-8")
    need(len(data) <= MAX_BODY, 413, "simulator_limit")
    return data


def closed(value, required, optional=()):
    need(isinstance(value, dict) and set(required) <= value.keys()
         and value.keys() <= set(required) | set(optional))
    return value


def integer(value, low=0, high=99999999):
    need(isinstance(value, str) and re.fullmatch(r"0|[1-9][0-9]{0,8}", value) is not None)
    number = int(value)
    need(low <= number <= high)
    return number


def json_object(raw):
    need(len(raw) <= MAX_BODY, 413, "simulator_limit")
    def pairs(items):
        out = {}
        for key, value in items:
            need(key not in out)
            out[key] = value
        return out
    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs,
                           parse_constant=lambda _: (_ for _ in ()).throw(SimulatorError()))
    except (UnicodeError, ValueError, RecursionError) as error:
        raise SimulatorError() from error
    need(isinstance(value, dict))
    return value


def pairs(raw):
    need(len(raw) <= MAX_BODY, 413, "simulator_limit")
    try:
        text = raw.decode("ascii")
        need(re.search(r"%(?![A-Fa-f0-9]{2})", text) is None)
        items = parse_qsl(text, keep_blank_values=True, strict_parsing=True,
                          encoding="utf-8", errors="strict", max_num_fields=48) if text else []
    except (UnicodeError, ValueError) as error:
        raise SimulatorError() from error
    out = {}
    for key, value in items:
        need(key not in out and 0 < len(key) <= 128 and len(value) <= 512)
        need(all(ord(c) >= 32 and ord(c) != 127 for c in key + value))
        out[key] = value
    return out


def metadata(form):
    result = {}
    other = {}
    for key, value in form.items():
        match = re.fullmatch(r"metadata\[([A-Za-z0-9_]{1,64})\]", key)
        if match:
            need(len(result) < 16 and len(value) <= 128)
            result[match[1]] = value
        else:
            other[key] = value
    return other, result


def intent_method(form, profile):
    field = profile.payment_method_types_field
    legacy_form = field == "payment_method_types[]"
    if not legacy_form:
        need("payment_method_types[]" not in form and "payment_method_types" not in form,
             400, "payment_method_types_no_longer_supported")
    required = {"amount", "currency"}
    if profile.default_payment_method is None:
        required.add(field)
    if profile.require_capture_method:
        required.add("capture_method")
    closed(form, required, {field, "capture_method", "application_fee_amount", "customer", "setup_future_usage"})
    method = form.get(field, profile.default_payment_method)
    need(method in profile.payment_method_types, 400, "simulator_invalid" if legacy_form else "simulator_unsupported")
    return method


def session(form):
    closed(form, {"account"}, {key for purpose, component in PURPOSES.items() if purpose != "card_payment"
                              for key in [f"components[{component}][enabled]",
                                          *[f"components[{component}][features][{f}]" for f in FEATURES[purpose]]]})
    selected = [purpose for purpose, component in PURPOSES.items()
                if form.get(f"components[{component}][enabled]") == "true"]
    need(len(selected) == 1 and selected[0] != "card_payment")
    purpose = selected[0]
    component = PURPOSES[purpose]
    expected = {"account":form["account"], f"components[{component}][enabled]":"true"}
    expected.update({f"components[{component}][features][{key}]":str(value).lower()
                     for key, value in FEATURES[purpose].items()})
    if purpose == "onboarding":
        # Existing onboarding sends only enabled=true; never permit auth disabling.
        expected.pop(f"components[{component}][features][disable_stripe_user_authentication]")
        optional = f"components[{component}][features][disable_stripe_user_authentication]"
        if optional in form:
            expected[optional] = "false"
    need(form == expected)
    return purpose, {component:{"enabled":True,"features":dict(FEATURES[purpose])}}


def route(method, target, raw):
    need(isinstance(target, str) and len(target) <= 2048)
    parsed = urlsplit(target)
    need(not parsed.scheme and not parsed.netloc and not parsed.fragment and not target.endswith("#"))
    path = parsed.path
    need(path.startswith("/") and "%" not in path and "//" not in path)
    routes = [
        ("POST", r"/v2/core/accounts", "account_v2_create"),
        ("GET", r"/v2/core/accounts/(acct_LocalSim[0-9a-f]{32})", "account_v2_get"),
        ("GET", r"/v2/core/events/(evt_LocalSim[0-9a-f]{32})", "event_v2_get"),
        ("POST", r"/v1/customers", "customer_create"),
        ("POST", r"/v1/checkout/sessions", "checkout_create"),
        ("GET", r"/v1/checkout/sessions", "billing_session_list"),
        ("POST", r"/v1/billing_portal/sessions", "portal_create"),
        ("POST", r"/v1/billing_portal/configurations", "portal_configuration_create"),
        ("GET", r"/v1/billing_portal/configurations", "billing_portal_configuration_list"),
        ("GET", r"/v1/billing_portal/configurations/(bpc_LocalSim[0-9a-f]{32})", "billing_portal_configuration_get"),
        ("GET", r"/v1/balance", "balance_get"),
        ("GET", r"/v1/balance_transactions", "balance_transaction_list"),
        ("GET", r"/v1/balance_transactions/(txn_LocalSim[0-9a-f]{32})", "balance_transaction_get"),
        ("GET", r"/v1/tax/settings", "tax_settings_get"),
        ("POST", r"/v1/payouts", "payout_create"),
        ("POST", r"/v1/payouts/instant_quotes", "payout_quote_create"),
        ("GET", rf"/v1/payouts/instant_quotes/({PAYOUT_QUOTE_ID})", "billing_payout_quote_get"),
        ("GET", r"/v1/payouts", "billing_payout_list"),
        ("GET", r"/v1/payouts/(po_LocalSim[0-9a-f]{32})", "billing_payout_get"),
        ("POST", r"/v1/payouts/(po_LocalSim[0-9a-f]{32})/cancel", "payout_cancel"),
        ("GET", r"/v1/customers/(cus_LocalSim[0-9a-f]{32})", "billing_customer_get"),
        ("GET", r"/v1/products/(prod_LocalSim[0-9a-f]{32})", "billing_product_get"),
        ("GET", r"/v1/prices/(price_LocalSim[0-9a-f]{32})", "billing_price_get"),
        ("GET", r"/v1/subscriptions/(sub_LocalSim[0-9a-f]{32})", "billing_subscription_get"),
        ("GET", r"/v1/subscriptions", "billing_subscription_list"),
        ("GET", r"/v1/checkout/sessions/(cs_LocalSim[0-9a-f]{32})", "billing_session_get"),
        ("GET", r"/v1/invoices/(in_LocalSim[0-9a-f]{32})", "billing_invoice_get"),
        ("GET", r"/v1/invoices/(in_LocalSim[0-9a-f]{32})/lines", "billing_line_list"),
        ("GET", r"/v1/invoices", "billing_invoice_list"),
        ("GET", r"/v1/invoice_payments", "billing_invoice_payment_list"),
        ("GET", r"/v1/charges", "billing_charge_list"),
        ("GET", r"/v1/disputes", "billing_dispute_list"),
        ("GET", r"/v1/disputes/(dp_LocalSim[0-9a-f]{32})", "billing_dispute_get"),
        ("GET", r"/v1/mandates/(mandate_LocalSim[0-9a-f]{32})", "billing_mandate_get"),
        ("GET", r"/v1/promotion_codes/(promo_LocalSim[0-9a-f]{32})", "billing_promotion_get"),
        ("GET", r"/v1/promotion_codes", "billing_promotion_list"),
        ("POST", r"/v1/accounts", "account_create"),
        ("GET", r"/v1/accounts/(acct_LocalSim[0-9a-f]{32})", "account_get"),
        ("POST", r"/v1/account_sessions", "session"),
        ("POST", r"/v1/payment_intents", "intent_create"),
        ("GET", r"/v1/payment_intents/(pi_LocalSim[0-9a-f]{32})", "intent_get"),
        ("POST", r"/v1/payment_intents/(pi_LocalSim[0-9a-f]{32})/(capture|cancel|confirm)", "intent_action"),
        ("GET", rf"/v1/payment_methods/({SAVED_CARD_ID})", "billing_payment_method_get"),
        ("POST", r"/v1/payment_intents/(pi_LocalSim[0-9a-f]{32})/verify_microdeposits", "bank_verify"),
        ("GET", r"/v1/charges/(ch_LocalSim[0-9a-f]{32})", "charge_get"),
        ("POST", r"/v1/refunds", "refund_create"),
        ("GET", r"/v1/refunds/(re_LocalSim[0-9a-f]{32})", "refund_get"),
        ("GET", r"/v1/application_fees/(fee_LocalSim[0-9a-f]{32})", "fee_get"),
        ("POST", r"/v1/application_fees/(fee_LocalSim[0-9a-f]{32})/refunds", "fee_refund_create"),
        ("GET", r"/v1/application_fees/(fee_LocalSim[0-9a-f]{32})/refunds/(fr_LocalSim[0-9a-f]{32})", "fee_refund_get"),
    ]
    found = next(((kind, match.groups()) for verb, pattern, kind in routes
                  if verb == method and (match := re.fullmatch(pattern, path))), None)
    need(found is not None, 400, "simulator_unsupported")
    kind, args = found
    query = pairs(parsed.query.encode("ascii")) if parsed.query else {}
    allowed = {"intent_get":{"expand[]":"latest_charge"},
               "charge_get":{"expand[]":"balance_transaction"},
               "refund_get":{"expand[]":"balance_transaction"},
               "billing_session_get":{"expand[]":"discounts"},
               "billing_subscription_get":{"expand[]":"discounts"},
               "billing_invoice_get":{"expand[]":"discounts"}}.get(kind, {})
    list_fields = {
        "billing_subscription_list":{"customer","status","limit","starting_after"},
        "billing_invoice_list":{"subscription","customer","limit","starting_after"},
        "billing_invoice_payment_list":{"invoice","status","payment[type]","payment[payment_intent]","limit","starting_after"},
        "billing_charge_list":{"payment_intent","limit","starting_after"},
        "billing_dispute_list":{"charge","limit","starting_after"},
        "billing_line_list":{"limit","starting_after"},
        "billing_session_list":{"customer","limit","starting_after"},
        "billing_portal_configuration_list":{"active","limit","starting_after"},
        "billing_payout_list":{"status","limit","starting_after"},
        "billing_promotion_list":{"active","code","limit","starting_after"},
    }
    if kind == "balance_transaction_list":
        closed(query,{"payout"},{"limit","starting_after"})
        need(re.fullmatch(r"po_LocalSim[0-9a-f]{32}",query["payout"]) is not None)
    elif kind == "account_v2_get":
        need(all(re.fullmatch(r"include\[[0-9]+\]",key) for key in query))
        indexes = sorted(int(key[8:-1]) for key in query)
        need(indexes == list(range(len(indexes))) and len(indexes) <= 3)
        need(all(value in {"identity","configuration.merchant","requirements"} for value in query.values()))
    elif kind in list_fields:
        closed(query,set(),list_fields[kind])
    elif kind == "account_v2_create":
        need(not parsed.query)
    else:
        need(not query or query == allowed)
    need("?" not in target or bool(parsed.query))
    if method == "GET":
        need(raw == b"")
        form = query
    else:
        need(not parsed.query)
        form = json_object(raw) if kind == "account_v2_create" else pairs(raw)
    return kind, args, form


def account_projection(account_id, meta, ready, now):
    return {"id":account_id,"object":"account","country":"US","default_currency":"usd",
            "controller":json.loads(json.dumps(CONTROLLER)), "metadata":meta,
            "charges_enabled":ready,"payouts_enabled":ready,"details_submitted":ready,
            "capabilities":{"card_payments":"active" if ready else "inactive","transfers":"active" if ready else "inactive"},
            "requirements":{"disabled_reason":None if ready else "requirements.past_due"},
            "created":int(now)}
