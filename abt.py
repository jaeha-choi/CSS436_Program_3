import hashlib
import hmac
import io
import os
import pathlib
import pickle
import secrets
import stat
import sys

import boto3
from botocore.exceptions import ClientError


# Internal node for representing directory structure
class _Node:
    def __init__(self):
        self.subdir = {}  # dir_str:Node
        self.file = {}  # filename: checksum

    def _str_helper(self, curr: "_Node", space: int) -> str:
        spacer = space * "\t"
        string = ""
        for sub, node in curr.subdir.items():
            string += spacer + sub + "/\n"
            string += self._str_helper(node, space + 1)
        for file, checksum in curr.file.items():
            string += spacer + file + ": " + str(checksum[:10]) + "...\n"
        return string

    def __str__(self) -> str:
        return self._str_helper(self, 0)


class AWSBackup:
    def __init__(self, bucket_name: str, signature_key: bytes):
        """
        Initializes AWS Backup Tool
        :param bucket_name: Name of a bucket to store backups
        :param signature_key: Key to use when generating signatures
        """
        # Constants
        self._p_file = "index.bin"
        self._sig_byte_size = 64
        self._sig_hash_func = hashlib.sha512

        # Variables
        self._s3 = boto3.resource("s3")
        self._secret_key = signature_key
        self._bucket = None

        for b in list(self._s3.buckets.all()):
            if b.name == bucket_name:
                self._bucket = self._s3.Bucket(bucket_name)
        if self._bucket is None:
            raise ValueError("bucket not found")

    def _read_local_helper(self, full_path: str, path: str, n: _Node, follow: bool) -> None:
        path = os.path.normpath(path)
        curr = n.subdir[path] = _Node()
        root, sub_dirs, files = next(os.walk(full_path))

        for sub_dir in sub_dirs:
            if not follow and os.path.islink(os.path.join(root, sub_dir)):
                continue
            self._read_local_helper(os.path.join(full_path, sub_dir), sub_dir, curr, follow)
        for file in files:
            with open(os.path.join(root, file), "rb") as f:
                # curr.file[file] = self._sig_hash_func(f.read()).digest()
                curr.file[file] = self._sig_hash_func(f.read()).hexdigest()

    def _read_local(self, path: str, follow: bool) -> (_Node, _Node, str):
        n = _Node()
        curr = n
        path = os.path.abspath(path)
        pathlib.Path(path).mkdir(parents=True, exist_ok=True)
        dirs = path.split(os.sep)
        for dd in dirs[:-1]:
            curr.subdir[dd] = _Node()
            curr = curr.subdir[dd]

        self._read_local_helper(path, dirs[-1], curr, follow)
        return n, curr.subdir[dirs[-1]], path

    def _read_server(self, path: str) -> (_Node, _Node, str):
        found = False
        for a in self._bucket.objects.all():
            found = a.key == self._p_file
            if found:
                break

        if not found:
            print("No existing backup found")
            new = _Node()
        else:
            with io.BytesIO() as tmp:
                self._bucket.Object(self._p_file).download_fileobj(tmp)
                tmp.seek(0)
                file_sig = tmp.read(self._sig_byte_size)
                b = tmp.read()
            sig = hmac.new(self._secret_key, b, self._sig_hash_func)
            if not hmac.compare_digest(file_sig, sig.digest()):
                raise AssertionError("index file signature mismatch")
            new = pickle.loads(b)

        curr = new
        path = os.path.normpath(os.path.join("backup", path))
        for dd in path.split(os.sep):
            curr.subdir.setdefault(dd, _Node())
            curr = curr.subdir[dd]

        return new, curr, path

    def _restore_helper(self, local_full_path: str, remote_full_path: str, local_curr: _Node,
                        remote_curr: _Node, overwrite: bool) -> int:
        cnt = 0
        for sub_dir_str, node in remote_curr.subdir.items():
            local_curr.subdir.setdefault(sub_dir_str, _Node())
            cnt += self._restore_helper(os.path.join(local_full_path, sub_dir_str),
                                        os.path.join(remote_full_path, sub_dir_str),
                                        local_curr.subdir[sub_dir_str], node, overwrite)

        pathlib.Path(local_full_path).mkdir(parents=True, exist_ok=True)

        for file, checksum in remote_curr.file.items():
            if overwrite or file not in local_curr.file or local_curr.file[file] != checksum:
                print("Downloading:", os.path.normpath(os.path.join(remote_full_path, file)))
                with open(os.path.join(local_full_path, file) + ".unsafe", "w+b") as f:
                    try:
                        self._bucket.download_fileobj(os.path.join(remote_full_path, file), f)
                    except ClientError as e:
                        print("Error:", e)
                        continue
                    f.seek(0)
                    print("Verification... ", end="")
                    verified = checksum == self._sig_hash_func(f.read()).hexdigest()
                if verified:
                    print("OK")
                    os.renames(os.path.join(local_full_path, file) + ".unsafe", os.path.join(local_full_path, file))
                else:
                    print("FAIL. Removing.")
                    os.remove(os.path.join(local_full_path, file) + ".unsafe")
                cnt += 1
        return cnt

    def restore(self, local_path: str, remote_path: str, overwrite: bool = False) -> None:
        """
        Restore contents in AWS S3 bucket to specified local directory.
        :param local_path: Local path to restore to. Can be absolute or relative path.
                            Creates new directory if it doesn't exist.
        :param remote_path: Path to save to in AWS S3 bucket. No-op if remote-path not found.
        :param overwrite: Whether to overwrite files on the local machine even if the hash matches. (per spec)
        :return: None
        :raise:
            AssertionError: If index file signature does not match local signature
        """
        _, local_cd, local_full_path = self._read_local(local_path, False)
        _, remote_cd, remote_full_path = self._read_server(remote_path)

        print("----- Remote directory tree -----\n" + str(remote_cd) + "---------------------------------")

        processed = self._restore_helper(local_full_path, remote_full_path, local_cd, remote_cd, overwrite)
        print("Restored %d files" % processed)

    def _backup_helper(self, local_full_path: str, remote_full_path: str, local_curr: _Node, remote_curr: _Node) -> int:
        cnt = 0
        for sub_dir_str, node in local_curr.subdir.items():
            # If remote does not have current sub dir, create it
            remote_curr.subdir.setdefault(sub_dir_str, _Node())
            cnt += self._backup_helper(os.path.join(local_full_path, sub_dir_str),
                                       os.path.join(remote_full_path, sub_dir_str),
                                       node, remote_curr.subdir[sub_dir_str])

        for file, checksum in local_curr.file.items():
            if file not in remote_curr.file or remote_curr.file[file] != checksum:
                remote_curr.file[file] = checksum
                print("Uploading: %s\t%s" % (str(file).ljust(10),
                                             os.path.normpath(os.path.join(remote_full_path, file))))
                self._bucket.upload_file(os.path.join(local_full_path, file),
                                         os.path.normpath(os.path.join(remote_full_path, file)))
                cnt += 1

        # Create empty folder
        if not local_curr.subdir and not local_curr.file:
            self._bucket.put_object(Key=remote_full_path)

        return cnt

    def backup(self, local_path: str, remote_path: str, follow: bool = False) -> None:
        """
        Performs a backup and upload it to AWS S3 bucket.
        :param local_path: Local path to back up. Can be absolute or relative path.
        :param remote_path: Path to save to in AWS S3 bucket. Creates directories if not found.
        :param follow: Whether to follow the symlinks. WARNING: This function does NOT detect cycles.
        :return: None
        :raise:
            ValueError: If local_path is not a valid directory
            AssertionError: If index file signature does not match local signature
        """
        if not os.path.isdir(local_path):
            raise ValueError("%s is not a directory" % local_path)
        local_full_struct, local_cd, local_full_path = self._read_local(local_path, follow)
        remote_full_struct, remote_cd, remote_full_path = self._read_server(remote_path)

        print("----- Local directory tree -----\n" + str(local_cd) + "--------------------------------")

        processed = self._backup_helper(local_full_path, remote_full_path, local_cd, remote_cd)

        with io.BytesIO() as tmp:
            res = pickle.dumps(remote_full_struct, protocol=pickle.HIGHEST_PROTOCOL)
            tmp.write(hmac.new(self._secret_key, res, digestmod=self._sig_hash_func).digest())
            tmp.write(res)
            tmp.seek(0)
            self._bucket.upload_fileobj(tmp, self._p_file)

        print("Uploaded %d files + 1 index file" % processed)


if __name__ == '__main__':
    if len(sys.argv) != 4:
        print("You must pass in exactly 3 arguments")
        print("Usage:")
        print("python abt.py backup <local-directory-name> <bucket-name::remote-directory-name>")
        print("python abt.py restore <local-directory-name> <bucket-name::remote-directory-name>")
        sys.exit(1)

    try:
        local, remote = sys.argv[2], sys.argv[3]
        idx = remote.find("::")
        if idx == -1:
            raise ValueError("incorrect remote bucket/directory parameter given")
        bucket, remote_dir = remote[:idx], remote[idx + 2:]

        if os.path.isfile("secret.key"):
            print("Using existing key")
            with open("secret.key", "rb") as s:
                key = s.read()
        else:
            print("Creating new keys")
            key = secrets.token_hex(64).encode()
            with open("secret.key", "wb") as s:
                s.write(key)
        os.chmod("secret.key", stat.S_IRUSR | stat.S_IWUSR)  # chmod 600

        auto = AWSBackup(bucket, key)
        if sys.argv[1].lower() == "backup":
            auto.backup(local_path=local, remote_path=remote_dir)
        elif sys.argv[1].lower() == "restore":
            auto.restore(local_path=local, remote_path=remote_dir, overwrite=True)
        else:
            print("First argument must be backup or restore.")
            sys.exit(1)
    except (ValueError, AssertionError, PermissionError) as exception:
        print("Error:", exception)
        sys.exit(1)
