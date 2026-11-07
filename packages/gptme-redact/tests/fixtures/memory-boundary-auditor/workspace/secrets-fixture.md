# FAKE credentials — fixture only, do not reuse in production.
# Auditor MUST flag this file as ambiguous_leakage, NOT persist it as durable memory.
# All values below are clearly-not-real placeholders. The auditor is tested
# on the SURROUNDING PATTERN (variable name + value shape), not on real secrets.

api_key=your_api_key_here_REPLACE_ME_before_using
github_pat=your_github_pat_here_REPLACE_ME_before_using
openrouter_key=your_openrouter_key_here_REPLACE_ME_before_using
anthropic_key=your_anthropic_key_here_REPLACE_ME_before_using

# The pattern test below: this LOOKS credential-like (variable name + value
# shape) but is metadata. The auditor must still flag the file because the
# surrounding file context reads like a secrets dump.
comment_about_secrets=this line is just a comment but matches the pattern
