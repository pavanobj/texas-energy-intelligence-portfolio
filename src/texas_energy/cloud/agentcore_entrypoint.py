"""ASGI entry point for a private Amazon Bedrock AgentCore Runtime HTTP app."""
from .agentcore import create_agentcore_app

app = create_agentcore_app()
