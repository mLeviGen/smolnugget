# smolnugget
a small utility for synchronizing a repository in GitHub and files in a directory (on the server)

python: 3.12.7+

`python3 snt.py --help` - all information

1. download the file to the folder where it will be cloned
2. start:
`python3 snt.py --token=<github_token> start --repo <name of this startup (like "2"/"ihor_bot")> --repo-url=<path/to/rep.git> --start-file=<file.py> --check-time=<seconds> --debug`
=> `python3 snt.py start --repo 2 --start-file=<file.py> --check-time=<seconds> --debug`

