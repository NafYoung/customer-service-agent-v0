# Offline routing cases (holdout v2 clusters A/B/C)

Independent of the frozen public 7-case regression set and of paid eval
bindings. These cases drive the public-demo **scripted** path (zero model
cost, zero writes) and lock tool routing that holdout v2 failed:

| Cluster | Case | Required route |
|---|---|---|
| A inventory | `route_inventory_sku_size` | only `get_inventory` |
| B eligibility-before-prepare | `route_cancel_eligibility_before_prepare` | `check_action_eligibility` before `prepare_cancel_order` |
| C policy-before-injection | `route_policy_search_ignores_injection` | `search_policy`; no prepare |

This is **not** a model evaluation and must not be reported as one.
Do not rerun holdout v1/v2.

```bash
.venv/bin/python evals/run_shadow_offline.py --case-dir evals/readonly_routing_cases
```
