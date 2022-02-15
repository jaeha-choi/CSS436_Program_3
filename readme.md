# AWS Backup Tool

#### Author: Jaeha Choi

## Description

AWS Backup Tool is a proof-of-concept code to demonstrate how backup/restore can be performed with AWS S3 while
preventing users from downloading files that are altered/modified by malicious users.

## Requirements

- Configured `~/.aws/credentials` with AWS credentials.
    - Click [this link](https://docs.aws.amazon.com/cli/latest/userguide/cli-configure-files.html) for more information.

- `Python 3.8.12`: (Other versions are not tested, but could be functional)

- Dependencies
    - `boto3 1.20.52`
    - `botocore 1.23.52`

## Design

- If index file verification fails (i.e. remote `index.bin` or local `secret.key` is modified), the ongoing operation is
  terminated.
- If other file verification fails (i.e. remote `index.bin` or local `secret.key` is modified), compromised files will
  not be restored, but the operation continues with the remaining files.
- All paths are normalized. E.g. `foo/./bar/foo/..` is replaced to `foo/bar`
- All files are stored under `backup` folder in AWS, even when saved at the root directory. This is to organize the
  directory structure, as well as to prevent `index.bin` file from getting overwritten.
- If the program gets executed as a standalone mode, it creates `secret.key` which contains a key used for index
  signatures

- Can be configured to follow symlinks with a parameter.
    - Warning: Cycle detection is not implemented. The program could be stuck in an infinite loop.

- This project is designed to be functional as both a standalone executable and a library.

- This program is NOT designed to encrypt files. (i.e. can be visible from the internet if public scope is used)

- (spec) Empty directories are visible on AWS
- (spec) Only directory paths are passed in as arguments
- (spec) AWS credentials must be in `~/.aws/credentials`, and should not be provided by the user
- (spec) Standalone executable overwrite files on the local machine even if the hash matches

## Usage
1. Backup: `python abt.py backup local-directory-name bucket-name::remote-directory-name`
2. Restore: `python abt.py restore local-directory-name bucket-name::remote-directory-name`


## Examples

1. Backup `test` directory in `test-remote`

```shell
> python abt.py backup test css436-prog-3::test-remote
Creating new keys
No existing backup found
----- Local directory tree -----
dir 1/
	dir 1.1/
		file_1.1.1: 07a42a96db...
		file_1.1.2: 07a42a96db...
	dir 1.2/
		file_1.2.1: 644c7b649d...
		file_1.2.2: 644c7b649d...
dir 2/
	file_2.1: 30ea36a6a7...
	file_2.2: 30ea36a6a7...
dir 3/
dir 4/
	dir 4.1/
		file_4.1.1: 0d9219cd1f...
	file_4: 0d9219cd1f...
--------------------------------
Uploading: file_1.1.1	backup/test-remote/dir 1/dir 1.1/file_1.1.1
Uploading: file_1.1.2	backup/test-remote/dir 1/dir 1.1/file_1.1.2
Uploading: file_1.2.1	backup/test-remote/dir 1/dir 1.2/file_1.2.1
Uploading: file_1.2.2	backup/test-remote/dir 1/dir 1.2/file_1.2.2
Uploading: file_2.1  	backup/test-remote/dir 2/file_2.1
Uploading: file_2.2  	backup/test-remote/dir 2/file_2.2
Uploading: file_4.1.1	backup/test-remote/dir 4/dir 4.1/file_4.1.1
Uploading: file_4    	backup/test-remote/dir 4/file_4
Uploaded 8 files + 1 index file
```

2. Restore `test-remote` to `test-restored`

```shell
> python abt.py restore test-restored css436-prog-3::test-remote
Using existing key
----- Remote directory tree -----
dir 1/
	dir 1.1/
		file_1.1.1: 07a42a96db...
		file_1.1.2: 07a42a96db...
	dir 1.2/
		file_1.2.1: 644c7b649d...
		file_1.2.2: 644c7b649d...
dir 2/
	file_2.1: 30ea36a6a7...
	file_2.2: 30ea36a6a7...
dir 3/
dir 4/
	dir 4.1/
		file_4.1.1: 0d9219cd1f...
	file_4: 0d9219cd1f...
---------------------------------
Downloading: backup/test-remote/dir 1/dir 1.1/file_1.1.1
Verification... OK
Downloading: backup/test-remote/dir 1/dir 1.1/file_1.1.2
Verification... OK
Downloading: backup/test-remote/dir 1/dir 1.2/file_1.2.1
Verification... OK
Downloading: backup/test-remote/dir 1/dir 1.2/file_1.2.2
Verification... OK
Downloading: backup/test-remote/dir 2/file_2.1
Verification... OK
Downloading: backup/test-remote/dir 2/file_2.2
Verification... OK
Downloading: backup/test-remote/dir 4/dir 4.1/file_4.1.1
Verification... OK
Downloading: backup/test-remote/dir 4/file_4
Verification... OK
Restored 8 files
```

3. Restore `test-remote/dir 1/dir 1.1/` to `test-restored-2`

```shell
> python abt.py restore test-restored-2 "css436-prog-3::test-remote/dir 1/dir 1.1/"
Using existing key
----- Remote directory tree -----
file_1.1.1: 07a42a96db...
file_1.1.2: 07a42a96db...
---------------------------------
Downloading: backup/test-remote/dir 1/dir 1.1/file_1.1.1
Verification... OK
Downloading: backup/test-remote/dir 1/dir 1.1/file_1.1.2
Verification... OK
Restored 2 files
```