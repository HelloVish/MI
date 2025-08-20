import os
import uuid
from typing import Dict, Optional

from kubernetes import client, config

class BotPodCreator:
    def __init__(self, namespace: str = "mi"):
        # Use in-cluster config
        config.load_incluster_config()
        print("Using in-cluster config")

        self.v1 = client.CoreV1Api()
        self.namespace = namespace

        # App metadata
        self.app_name = os.getenv('CUBER_APP_NAME', 'mi')
        self.app_version = os.getenv('CUBER_RELEASE_VERSION')
        if not self.app_version:
            raise ValueError("CUBER_RELEASE_VERSION environment variable is required")
        self.app_instance = f"{self.app_name}-{self.app_version.split('-')[-1]}"
        self.image = "514115671611.dkr.ecr.ap-south-1.amazonaws.com/mi-app:latest"

        # Validate and store resource limits and requests
        self.resources = self._get_resource_limits()

    def _get_resource_limits(self) -> Dict:
        def clean(key: str, default: str) -> str:
            val = os.getenv(key, default).strip()
            return val

        cpu_request = clean("BOT_CPU_REQUEST", "250m")
        memory_request = clean("BOT_MEMORY_REQUEST", "500Mi")
        storage_request = clean("BOT_EPHEMERAL_STORAGE_REQUEST", "1Gi")

        memory_limit = clean("BOT_MEMORY_LIMIT", "500Mi")
        storage_limit = clean("BOT_EPHEMERAL_STORAGE_LIMIT", "1Gi")

        # Kubernetes requires valid quantity strings: 250m, 1Gi, etc.
        valid_regex = r'^([+-]?[0-9.]+)([eEinumkKMGTP]*[-+]?[0-9]*)$'
        import re
        for val in [cpu_request, memory_request, storage_request, memory_limit, storage_limit]:
            if not re.match(valid_regex, val):
                raise ValueError(f"Invalid resource quantity format: {val}")

        return {
            "requests": {
                "cpu": cpu_request,
                "memory": memory_request,
                "ephemeral-storage": storage_request
            },
            "limits": {
                "memory": memory_limit,
                "ephemeral-storage": storage_limit
            }
        }

    def create_bot_pod(self, bot_id: int, bot_name: Optional[str] = None) -> Dict:
        if bot_name is None:
            bot_name = f"bot-{bot_id}-{uuid.uuid4().hex[:8]}"

        bot_cmd = f"python manage.py run_bot --botid {bot_id}"
        command = ["/bin/bash", "-c", f"/opt/bin/entrypoint.sh && {bot_cmd}"]

        labels = {
            "app.kubernetes.io/name": self.app_name,
            "app.kubernetes.io/instance": self.app_instance,
            "app.kubernetes.io/version": self.app_version,
            "app.kubernetes.io/managed-by": "cuber",
            "app": "bot-proc"
        }

        pod = client.V1Pod(
            metadata=client.V1ObjectMeta(
                name=bot_name,
                namespace=self.namespace,
                labels=labels
            ),
            spec=client.V1PodSpec(
                containers=[
                    client.V1Container(
                        name="bot-proc",
                        image=self.image,
                        image_pull_policy="Always",
                        command=command,
                        resources=client.V1ResourceRequirements(
                            requests=self.resources["requests"],
                            limits=self.resources["limits"]
                        ),
                        env_from=[
                            client.V1EnvFromSource(
                                config_map_ref=client.V1ConfigMapEnvSource(name="mi-env")
                            ),
                            client.V1EnvFromSource(
                                secret_ref=client.V1SecretEnvSource(name="app-secrets")
                            )
                        ],
                        env=[]
                    )
                ],
                restart_policy="Never",
                image_pull_secrets=[
                    client.V1LocalObjectReference(name="regcred")
                ],
                termination_grace_period_seconds=60,
                tolerations=[
                    client.V1Toleration(
                        key="node.kubernetes.io/not-ready",
                        operator="Exists",
                        effect="NoExecute",
                        toleration_seconds=900
                    ),
                    client.V1Toleration(
                        key="node.kubernetes.io/unreachable",
                        operator="Exists",
                        effect="NoExecute",
                        toleration_seconds=900
                    )
                ]
            )
        )

        try:
            api_response = self.v1.create_namespaced_pod(
                namespace=self.namespace,
                body=pod
            )
            return {
                "name": api_response.metadata.name,
                "status": api_response.status.phase,
                "created": True,
                "image": self.image,
                "app_instance": self.app_instance,
                "app_version": self.app_version
            }

        except client.ApiException as e:
            return {
                "name": bot_name,
                "status": "Error",
                "created": False,
                "error": str(e)
            }

    def delete_bot_pod(self, pod_name: str) -> Dict:
        try:
            self.v1.delete_namespaced_pod(
                name=pod_name,
                namespace=self.namespace,
                grace_period_seconds=60
            )
            return {"deleted": True}
        except client.ApiException as e:
            return {
                "deleted": False,
                "error": str(e)
            }
