from schemas.base_schemas import BaseRequest, BaseResponse


class SiteConnectRequest(BaseRequest):
    platform: str  # wordpress | shopify
    # per-platform shape, documented on the consuming adapter:
    #   wordpress: {base_url, username, app_password}
    #   shopify:   {shop_domain, access_token}
    credentials: dict


class PickPropertyRequest(BaseRequest):
    # gsc: a siteUrl ("sc-domain:example.com" / "https://example.com/");
    # ga4: a numeric property id
    property: str


class SiteStatusResponse(BaseResponse):
    platform: str
    status: str  # pending | connected | error | disconnected
    connected_at: str | None


class ProviderStatusResponse(BaseResponse):
    provider: str  # gsc | ga4 | gbp | calls
    status: str
    connected_at: str | None


class ConnectionsResponse(BaseResponse):
    """The Connect page's status card: the write-side site + the read-side
    provider connections, secrets never included."""

    site: SiteStatusResponse | None
    providers: list[ProviderStatusResponse]
