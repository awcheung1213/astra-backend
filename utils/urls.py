from urllib.parse import urlparse


def domain_from_url(url: str) -> str:
    """Extract the host portion of a url (scheme, path, query and port stripped).

    e.g. "https://www.example.com/article?x=1" -> "www.example.com"
    """
    # Prefix a scheme-less url with "//" so urlparse treats it as a network
    # location rather than a path.
    netloc = urlparse(url if "//" in url else f"//{url}").netloc
    return netloc.split(":")[0]
