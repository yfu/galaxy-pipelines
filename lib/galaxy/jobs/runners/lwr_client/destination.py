
from re import match


def url_to_destination_params(url):
    """Convert a legacy runner URL to a job destination

    >>> params_simple = url_to_destination_params("http://localhost:8913/")
    >>> params_simple["url"]
    'http://localhost:8913/'
    >>> params_simple["private_token"] is None
    True
    >>> advanced_url = "https://1234x@example.com:8914/managers/longqueue"
    >>> params_advanced = url_to_destination_params(advanced_url)
    >>> params_advanced["url"]
    'https://example.com:8914/managers/longqueue/'
    >>> params_advanced["private_token"]
    '1234x'
    >>> runner_url = "lwr://http://localhost:8913/"
    >>> runner_params = url_to_destination_params(runner_url)
    >>> runner_params['url']
    'http://localhost:8913/'
    """

    if url.startswith("lwr://"):
        url = url[len("lwr://"):]

    if not url.endswith("/"):
        url += "/"

    ## Check for private token embedded in the URL. A URL of the form
    ## https://moo@cow:8913 will try to contact https://cow:8913
    ## with a private key of moo
    private_token_format = "https?://(.*)@.*/?"
    private_token_match = match(private_token_format, url)
    private_token = None
    if private_token_match:
        private_token = private_token_match.group(1)
        url = url.replace("%s@" % private_token, '', 1)

    destination_args = {"url": url,
                        "private_token": private_token}

    return destination_args
