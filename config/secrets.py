"""
Single interface for secrets. Development: .env file. Production: AWS SSM Parameter Store.
Never read os.environ directly outside this module.
"""
import os
from functools import lru_cache


def _load_local() -> dict:
    try:
        from dotenv import load_dotenv
        load_dotenv()
    except ImportError:
        pass  # dotenv optional in non-dev environments
    return dict(os.environ)


def _load_ssm() -> dict:
    import boto3
    region = os.getenv("AWS_REGION", "us-east-1")
    ssm = boto3.client("ssm", region_name=region)
    prefix = "/gold-bot/prod/"
    paginator = ssm.get_paginator("get_parameters_by_path")
    result = {}
    for page in paginator.paginate(Path=prefix, WithDecryption=True, Recursive=True):
        for p in page["Parameters"]:
            key = p["Name"].replace(prefix, "")
            result[key] = p["Value"]
    return result


@lru_cache(maxsize=1)
def get_secrets() -> dict:
    if os.getenv("ENVIRONMENT") == "production":
        return _load_ssm()
    return _load_local()


def get(key: str) -> str:
    val = get_secrets().get(key)
    if not val:
        raise RuntimeError(f"Missing secret: {key}")
    return val
