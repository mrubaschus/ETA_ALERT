import base64
import json
import os
import boto3
import botocore.exceptions


class Storage:
    """
    A multi-purpose object storage.
    Can be used to store files, images, etc.
    """

    def __init__(self, credentials: dict[str, str]):
        self.client = boto3.client("s3", **credentials)
        self.bucket = os.environ["SamsaraFunctionStorageName"]

    def put(self, Key: str, Body: bytes, **kwargs):
        """
        Insert or overwrite an object.
        Kwargs are passed to the underlying boto3.client('s3').put_object().
        Returns the original boto3 response.
        """
        return self.client.put_object(
            Bucket=self.bucket,
            Key=Key,
            Body=Body,
            **kwargs,
        )

    def put_base64(self, Key: str, Base64: str, **kwargs):
        """
        Insert or overwrite an object from a base64 encoded string.
        Object will be stored as bytes, not as a string.
        Kwargs are passed to the underlying `boto3.client('s3').put_object()`.
        Returns the original boto3 response.
        """
        return self.put(Key, Body=base64.b64decode(Base64), **kwargs)

    def get(self, Key: str, **kwargs):
        """
        Get an object with it's metadata.
        Kwargs are passed to the underlying `boto3.client('s3').get_object()`.
        Returns the original boto3 response.
        """
        return self.client.get_object(
            Bucket=self.bucket,
            Key=Key,
            **kwargs,
        )

    def get_body(self, Key: str, **kwargs) -> bytes:
        """
        Get an object's body.
        Returns bytes.
        Kwargs are passed to the underlying `boto3.client('s3').get_object()`.
        """
        return self.get(Key, **kwargs)["Body"].read()

    def get_body_base64(self, Key: str, **kwargs) -> str:
        """
        Get an object's body as a base64 encoded string.
        Expects the object to be stored as bytes.
        Kwargs are passed to the underlying `boto3.client('s3').get_object()`.
        """
        body = self.get_body(Key, **kwargs)
        return base64.b64encode(body).decode("utf-8")

    def delete(self, Key: str, **kwargs):
        """
        Delete an object.
        Kwargs are passed to the underlying `boto3.client('s3').delete_object()`.
        Returns the original boto3 response.
        """
        return self.client.delete_object(
            Bucket=self.bucket,
            Key=Key,
            **kwargs,
        )

    def list_objects(
        self,
        Prefix: str = "",
        **kwargs,
    ):
        """
        List objects in the bucket with bucket and object metadata.
        Kwargs are passed to the underlying `boto3.client('s3').list_objects_v2()`.
        Returns the original boto3 response.
        """
        return self.client.list_objects_v2(
            Bucket=self.bucket,
            Prefix=Prefix,
            **kwargs,
        )

    def list_contents(
        self,
        Prefix: str = "",
        **kwargs,
    ):
        """
        List object keys in the bucket.
        Kwargs are passed to the underlying `boto3.client('s3').list_objects_v2()`.
        Returns a list of keys.
        """
        res = self.list_objects(Prefix=Prefix, **kwargs)
        if "Contents" not in res:
            return []
        return [obj["Key"] for obj in res["Contents"]]


class Database:
    """
    A database for storing key-value pairs.
    Uses S3 Storage as a backend.
    Keys are stored as `<namespace>/<key>`.

    For permanent storage, avoid clearing the namespace.
    For temporary storage, clear the namespace on startup.

    Considerations:
    - S3 is eventually consistent, so reads may not reflect the latest writes.
    - S3 is not optimized for low latency. Avoid using it for high-frequency reads/writes.
    - S3 PUT/GET/DELETE operations are free for the first 1M requests per month.
      After that, you'll be charged for each request.
    """

    def __init__(self, storage: Storage, namespace: str = "db"):
        self.storage = storage
        self.namespace = namespace

    def __key(self, key: str) -> str:
        return f"{self.namespace}/{key}"

    def put(self, key: str, value: str):
        return self.storage.put(Key=self.__key(key), Body=value.encode("utf-8"))

    def put_dict(self, key: str, value: dict):
        return self.put(key, json.dumps(value))

    def get(self, key: str) -> str | None:
        try:
            return self.storage.get_body(Key=self.__key(key)).decode("utf-8")
        except botocore.exceptions.ClientError as e:
            if e.response["Error"]["Code"] == "NoSuchKey":
                return None
            raise

    def get_dict(self, key: str) -> dict | None:
        value = self.get(key)
        if value is None:
            return None
        return json.loads(value)

    def keys(self) -> list[str]:
        """List all keys in the database (without namespace prefix)."""
        prefix = f"{self.namespace}/"
        all_keys = self.storage.list_contents(Prefix=prefix)
        return [k[len(prefix):] for k in all_keys]

    def delete(self, key: str):
        return self.storage.delete(Key=self.__key(key))


_credentials: None | dict[str, str] = None


def _clear_caches():
    """Clear all cached credentials and storage instances."""
    global _credentials, _storage, _databases
    _credentials = None
    _storage = None
    _databases = {}


def get_credentials(force_refresh=False) -> dict[str, str]:
    global _credentials
    if _credentials is not None and not force_refresh:
        return _credentials

    sts = boto3.client("sts")
    res = sts.assume_role(
        RoleArn=os.environ["SamsaraFunctionExecRoleArn"],
        RoleSessionName=os.environ["SamsaraFunctionName"],
    )
    _credentials = {
        "aws_access_key_id": res["Credentials"]["AccessKeyId"],
        "aws_secret_access_key": res["Credentials"]["SecretAccessKey"],
        "aws_session_token": res["Credentials"]["SessionToken"],
    }
    return _credentials


_storage: None | Storage = None


def get_storage(force_refresh=False) -> Storage:
    global _storage
    if _storage is not None and not force_refresh:
        return _storage

    _storage = Storage(get_credentials(force_refresh=force_refresh))
    return _storage


_databases: dict[str, Database] = {}


def get_database(namespace: str | None = None, force_refresh=False) -> Database:
    """
    Get a database instance.
    If `namespace` is `None`, the function name will be used.
    Namespace is used to prefix the storage keys used by the database.
    """
    if namespace is None:
        namespace = os.environ["SamsaraFunctionName"]

    global _databases
    if namespace in _databases and not force_refresh:
        return _databases[namespace]

    _databases[namespace] = Database(get_storage(force_refresh=force_refresh), namespace)
    return _databases[namespace]
