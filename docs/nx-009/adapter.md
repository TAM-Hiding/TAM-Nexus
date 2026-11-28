# Local inference adapter

NX-009 can submit one bounded chat completion to an operator-provisioned, single-model llama.cpp-compatible HTTP service. The coordinator connects through the fixed Linux domain socket `/run/tam-nexus-inference/inference.sock`. The model alias is `nexus-pilot`. The concrete adapter sends `POST /v1/chat/completions` with one fixed system instruction, the canonical reasoning input as the user message, JSON object response format, temperature zero, streaming disabled and at most 1,024 generated tokens. The payload digest covers all of those provider request fields. The operator reviews the exact body and recipient before disclosure. [llama.cpp server interface](https://github.com/ggml-org/llama.cpp/blob/master/tools/server/README.md)

## Provisioning boundary

The endpoint is optional. A live pilot requires a separately provisioned service under a distinct unprivileged Linux UID. Its dedicated directory immediately below system-managed `/run` must be a real directory owned by that UID, with group and other write bits clear. The socket must be a real socket owned by the same UID. The Nexus account needs directory traversal and socket connection permission. The operator's authorization binds the service UID. The client verifies the actual connected peer UID with `SO_PEERCRED` before sending headers or body.

The operator must establish that the service runs only the authorized local model with local weights, in a confined environment without network egress, coordinator-state access, operator files, credentials, external tools or unrestricted logging. A socket and UID authenticate the local recipient. They cannot attest its model weights, retention policy or behavior after receipt. Replacing a service under the same approved UID needs operator attention. Keep this pilot disabled where those service properties and the workstation sandbox have not been verified. Nexus does not start a service, install software or discover a model.

## Wire limits and reporting

The adapter accepts a single length-delimited, uncompressed JSON HTTP 200 response with at most 4,096 header bytes and 32,768 body bytes. It expects exactly one completed assistant choice, a JSON text response within the 8 KiB reasoning limit, and the NX-007 output contract. The two-second invocation deadline covers transport and response validation. A larger model or a cold load can exceed this limit. A lost or late reply leaves an uncertain request outcome. Closing the connection bounds the Nexus invocation; independent server compute or retention after receipt remains a provider concern.

Metadata retains the exact response body digest on successful parsing, elapsed milliseconds, optional server-reported prompt and completion token counts, and model identity only when the returned label matches the approved public alias. Numerical usage is advisory. The adapter has no API credential flow, URL selection, proxy, redirect, or model tool interface.

`mock-v1` constructs an HTTP-shaped response locally using the deterministic fixture investigator. It exercises the same output validator and consumes no model tokens. The transport tests also use a connected Unix socketpair and synthetic HTTP peer to check wire bytes, peer validation and deadlines. Real inference, deployment compatibility and physical workstation acceptance remain pending.
