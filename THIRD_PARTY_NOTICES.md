# Third-party notices

## Anthropic commerce-agents

`app/agent/tool_result_fencing.py` adapts the data-fencing primitives from
[`anthropics/commerce-agents`](https://github.com/anthropics/commerce-agents),
commit `fd4d59224ab96b43c6dc6888207c67b3bd5a24cf`, original file
`commerce-common/commerce_common/fencing.py`.

Copyright 2026 Anthropic PBC. Licensed under the Apache License, Version 2.0.
The RIVET adaptation removes presentation-only helpers and adds a fixed
server-tool provenance envelope. The license text is retained at
`LICENSES/Apache-2.0.txt`.
