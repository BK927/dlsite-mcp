# Security

Report security issues privately to the repository owner before public
disclosure. Never include access tokens or OAuth secrets in an issue.

Remote deployments should use HTTPS, a 32-character-or-longer random bearer
token, distinct OAuth login and signing secrets, strict allowed hosts/origins,
and a non-root container user. DLsite publisher text is untrusted input even
when returned through an official page.
