# import hmac
import hashlib
import os
import pathlib
import pickle
import sys
import tempfile

import boto3


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
            string += spacer + file + ": " + str(checksum) + "\n"
        return string

    def __str__(self) -> str:
        return self._str_helper(self, 0)


def _read_local_helper(full_path: str, path: str, n: _Node):
    path = os.path.normpath(path)
    curr = n.subdir[path] = _Node()
    root, sub_dirs, files = next(os.walk(full_path))

    for sub_dir in sub_dirs:
        # Uncomment to ignore symlink
        # if os.path.islink(os.path.join(full_path, sub_dir)):
        #     continue
        _read_local_helper(os.path.join(full_path, sub_dir), sub_dir, curr)
    for file in files:
        with open(os.path.join(root, file), "rb") as f:
            # curr.file[file] = hashlib.md5(f.read()).digest()
            curr.file[file] = hashlib.md5(f.read()).hexdigest()


def _read_local(path: str) -> (_Node, _Node, str):
    d = _Node()
    curr = d
    path = os.path.abspath(path)
    pathlib.Path(path).mkdir(parents=True, exist_ok=True)
    dirs = path.split(os.sep)
    for dd in dirs[:-1]:
        curr.subdir[dd] = _Node()
        curr = curr.subdir[dd]

    _read_local_helper(path, dirs[-1], curr)
    return d, curr.subdir[dirs[-1]], path


class AutoBackup:
    def __init__(self):
        self._p_file = "index.bin"
        self._bucket = None
        self._s3 = boto3.resource("s3")
        # self._secret_key = "*insert your secret signature key here*"

    def _read_server(self, path: str) -> (_Node, _Node, str):
        # TODO: verify pickle file
        found = False
        for a in self._bucket.objects.all():
            found = a.key == self._p_file
            if found:
                break

        if not found:
            print("No existing backup found")
            new = _Node()
        else:
            with tempfile.TemporaryFile() as f:
                self._bucket.Object(self._p_file).download_fileobj(f)
                f.seek(0)
                new = pickle.load(f)

        curr = new
        path = os.path.normpath(os.path.join("backup", path))
        dirs = path.split(os.sep)
        for dd in dirs:
            curr.subdir.setdefault(dd, _Node())
            curr = curr.subdir[dd]

        return new, curr, path

    def _restore_helper(self, local_full_path: str, remote_full_path: str, local_curr: _Node,
                        remote_curr: _Node) -> int:
        cnt = 0
        for sub_dir_str, node in remote_curr.subdir.items():
            local_curr.subdir.setdefault(sub_dir_str, _Node())
            cnt += self._restore_helper(os.path.join(local_full_path, sub_dir_str),
                                        os.path.join(remote_full_path, sub_dir_str),
                                        local_curr.subdir[sub_dir_str], node)

        pathlib.Path(local_full_path).mkdir(parents=True, exist_ok=True)

        for file, checksum in remote_curr.file.items():
            if file not in local_curr.file or local_curr.file[file] != checksum:
                print("Downloading: %s\t:%s" % (file, os.path.normpath(os.path.join(local_full_path, file))))
                self._bucket.download_file(os.path.join(remote_full_path, file), os.path.join(local_full_path, file))
                cnt += 1
        return cnt

    def restore(self, local_path: str, remote_path: str):
        _, local_cd, local_full_path = _read_local(local_path)
        _, remote_cd, remote_full_path = self._read_server(remote_path)
        processed = self._restore_helper(local_full_path, remote_full_path, local_cd, remote_cd)
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
                print("Uploading: %s\t%s" % (file, os.path.normpath(os.path.join(remote_full_path, file))))
                self._bucket.upload_file(os.path.join(local_full_path, file),
                                         os.path.normpath(os.path.join(remote_full_path, file)))
                cnt += 1

        # Create empty folder
        if not local_curr.subdir and not local_curr.file:
            self._bucket.put_object(Key=remote_full_path)

        return cnt

    def backup(self, local_path: str, remote_path: str):
        if not os.path.isdir(local_path):
            print("%s is not a directory" % local_path)
            return
        local_full_struct, local_cd, local_full_path = _read_local(local_path)
        remote_full_struct, remote_cd, remote_full_path = self._read_server(remote_path)

        # print(local_full_struct)
        # print(local_cd)
        # print(local_full_path)
        #
        # print("remote")
        # print(remote_full_struct)
        # print("remote current")
        # print(remote_cd)
        # print("remote full path")
        # print(remote_full_path)
        processed = self._backup_helper(local_full_path, remote_full_path, local_cd, remote_cd)

        # TODO: Add pickle signature
        with open(self._p_file, "wb") as f:
            pickle.dump(remote_full_struct, f, protocol=pickle.HIGHEST_PROTOCOL)
        self._bucket.upload_file(self._p_file, self._p_file)
        os.remove(self._p_file)
        print("Uploaded %d files + 1 index file" % processed)

    def find_bucket(self, bucket_name) -> bool:
        found = False
        for b in list(self._s3.buckets.all()):
            found = b.name == bucket_name
            if found:
                self._bucket = self._s3.Bucket(bucket_name)
                return found
        return found

    def run(self):
        bucket_name = "css436-prog-3"
        if not self.find_bucket(bucket_name):
            print("Bucket not found")
            return
        print("1. Backup")
        self.backup("./test", "./")

        print("2. Backup")
        self.backup("./test/dir 4/4", "./")

        print("3. Restore")
        self.restore("./test3", ".")


if __name__ == '__main__':
    # AutoBackup().run()
    auto = AutoBackup()
    if len(sys.argv) != 4:
        print("You must pass in exactly 2 arguments")
        print("Usage:")
        print("% backup <local-directory-name> <bucket-name::remote-directory-name>")
        print("% restore <local-directory-name> <bucket-name::remote-directory-name>")
        sys.exit(1)

    local, remote = sys.argv[2], sys.argv[3]
    idx = remote.find("::")
    bucket, remote_dir = remote[:idx], remote[idx + 2:]

    if not auto.find_bucket(bucket):
        print("Bucket not found")
        sys.exit(1)
    if sys.argv[1].lower() == "backup":
        auto.backup(local_path=local, remote_path=remote_dir)
    elif sys.argv[1].lower() == "restore":
        auto.restore(local_path=local, remote_path=remote_dir)
    else:
        print("First argument must be backup or restore.")
        sys.exit(1)
