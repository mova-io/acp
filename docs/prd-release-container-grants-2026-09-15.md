# Production Release container grants

Provide an explicit optional grants file for the private Release provisioner. Existing invocations keep exact source Blob Data Contributor cloning, including conditions, so staging rollout behavior is preserved.

The optional file lists container ARM scopes and reader/contributor access. Every selected scope must be inside an existing source Blob Data Contributor grant, in the intended ACP_BLOB_ACCOUNT and subscription. Reject account, resource-group, subscription and other-account scope selection; reject production grants to staging containers. A conditional source grant can only be retained at its identical scope and role; do not reinterpret or strip conditions while narrowing. Reject malformed, duplicate and unknown selections before any Azure mutation.

No existing identity grants, production configuration or staging state are changed by this implementation. Parent applies only after review and green CI. Tests cover permission subset checks and an offline provisioner flow proving exact selected grants precede activation. Coordinate any staging activation fix in this same prerequisite PR, not a competing provisioner edit.

Staging owner reproduced ARM activation HTTP 400: response-only `imageType` copied from ContainerApp show is rejected by the activation API. Strip only that field from the derived request, preserve the source definition, and test this behavior in the same PR. The staging owner exclusively applies its live recovery; this task performs no Azure mutation.

The staging owner also reproduced HTTP 400 for response-only `customMetricsSettings` on ContainerAppTemplate. Strip it from the derived activation template alongside container `imageType`. Preserve other source settings and original response objects.

Offline validation: 86 provisioner/lane/capacity/connection-budget/workflow tests passed, with no paid calls. Permission-subset tests exercise reader/contributor narrowing, sibling and scope-boundary rejection, missing-source evidence, duplicates, malformed input, conditional grant preservation/rejection, environment isolation and fake-Azure grant-before-activation order. Existing default exact conditional clone and dry-run behavior pass.
