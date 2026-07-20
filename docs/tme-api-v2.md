# TME API v2 — reference

Working reference for the TME (tme.eu / tme.pl) shopping-platform API, kept in the
repo because their published documentation sits behind a login and is not indexed.
The endpoint sections below are **generated from the OpenAPI 3.1 specification
embedded in TME's own documentation page**, so field names and types are authoritative
rather than transcribed by hand. Captured 2026-07-20.

Only `/products`, `/products/parameters` and `/products/files` are used today (by
`app/services/shops/tme.py`); the rest is documented so the next action against this
API doesn't need another round of discovery.

> **Version matters.** This is **v2**. API tokens generated after **2026-05-14** work
> only with v2. The old API (`api.tme.eu/Products/GetProducts.json`, HMAC-SHA1
> `ApiSignature`) is deprecated and shaped completely differently — most search
> results and third-party examples online still describe that one.

- Base URL: `https://api.tme.eu`
- Everything is JSON. Successful responses are `{"status": "OK", "data": {…}}`.

## Authentication

Two accounts are required: a customer account at tme.eu and a developer account at
developers.tme.eu. Register an application in the tme.eu customer panel ("Applications"),
which issues a temporary token; use it at developers.tme.eu to generate the private
key. The result is a pair:

- a **50-character token** — the Basic-auth *username*
- a **20-character application secret** — the Basic-auth *password*

Both are credentials. Redact both from logs and error messages.

```bash
curl --location 'https://api.tme.eu/auth/token' \
  --header 'Content-Type: application/x-www-form-urlencoded' \
  --header 'Authorization: Basic <base64(token:secret)>' \
  --data-urlencode 'grant_type=client_credentials'
```

```json
{ "access_token": "…", "token_type": "Bearer", "expires_in": 300, "refresh_token": "…" }
```

Then send `Authorization: Bearer <access_token>` on every request. **The token lives
300 seconds**, so cache it; a `refresh_token` grant exists but simply re-running
client-credentials is equivalent and simpler.

## Conventions and traps

Most of these cost real debugging. They are not obvious from the endpoint list.

**Language** is the `Accept-Language` header, and must be one of `/utils/languages`.
Unsupported or missing → English. Endpoints without translated data ignore it.
*Keep it `en` for ShelfOS*: a translated locale also translates parameter **names**,
which then stop matching English parameter labels and get silently dropped.

**Country** is a `country` query parameter (e.g. `country=PL`), not a header.

**Product symbols are TME's own identifiers, not manufacturer part numbers.** The MPN
comes back in `manufacturer_symbols`, which is **empty for some products** — fall back
to `symbol`. Passing an MPN as `symbols[]` simply finds nothing (verified: TME symbol
`681KD20JP10-YAG` resolves, its MPN `681KD20J-P10` does not). `/products` accepts
`mpns[]` as a separate parameter if you need lookup the other way round.

**Symbols must be 2–18 characters, and one out-of-range value fails the WHOLE
request** with `E_INPUT_PARAMS_VALIDATION_ERROR`, not just that entry. Filter before
sending when symbols come from an untrusted source such as a URL.

**Unknown-but-well-formed symbols are silently omitted** from the response rather than
erroring. Combined with the 50-symbol limit this makes the API a useful oracle: offer
several candidates in one call and see which come back.

**The symbol's position in a product URL is not fixed.** Both of these are real:

```
/pl/details/0603b104k500ct/kondensatory-mlcc-smd/walsin/       ← symbol first
/pl/details/mpp2/681kd20jp10-yag/yageo/681kd20j-p10/           ← symbol second
```

URLs carry it lower-cased while the API expects upper-case. Don't parse by index —
offer every segment (length-filtered) as `symbols[]` and let the API decide.

**Asset and document URLs are protocol-relative** (`//host/path`). Resolve them
against `https://www.tme.eu/` — note documents live on the **storefront** host, not
`api.tme.eu` — before handing them to any fetcher.

**Documents cannot be downloaded server-side.** `https://www.tme.eu/Document/…`
answers a non-browser GET with a Cloudflare interactive challenge (`403`,
`cf-mitigated: challenge`). No combination of User-Agent, Referer, `Accept-*` or
`Sec-Fetch-*` headers gets through — it requires JS and cookies by design. Treat a TME
datasheet as a **link**, not a file.

**There is no "datasheet" document type.** The closest is `DTE` ("Documentation") —
see the glossary below.

**Specs live in `/products/parameters`, not in `/products`.** The `description` field
is a compact human string (`"Capacitor: ceramic; 100nF; 50V; X7R; ±10%; 0603"`) and
often omits things the **category name** states plainly (that part is filed under
`"MLCC SMD capacitors"` — the only place the mounting appears). Treat `category.name`
as a data source, not just a label.

## Glossaries

### Document types (`/products/files` → `documents.elements[].type`)

| Code | Meaning |
| --- | --- |
| `DTE` | Documentation — the nearest thing to a datasheet |
| `INS` | Manual |
| `KCH` | Safety data sheet |
| `GWA` | Warranty |
| `INB` | Safety instructions |
| `MOV` | Video |
| `YTB` | YouTube video |
| `PRE` | Presentation |
| `SFT` | Software |

### Product statuses (`product_status[]`)

| Status | Meaning |
| --- | --- |
| `AVAILABLE_WHILE_STOCKS_LAST` | Available for sale while stocks last. |
| `CANNOT_BE_ORDERED` | Not available for sale in your country. |
| `DANGEROUS` | Dangerous goods; delivery restrictions apply (e.g. no air freight). |
| `EXTERNAL_WAREHOUSE` | Ships from an external warehouse. |
| `HARDLY_AVAILABLE` | Limited market availability. |
| `INVALID` | No information held; the API returns no PiP link for it. |
| `MOQ_VALID_WHILE_STOCKS_LAST` | The minimum order quantity may change once sold out. |
| `NEW` | New in the catalogue. |
| `NOT_IN_OFFER` | Withdrawn; stock, prices and delivery time are not meaningful. |
| `ONLY_FOR_SPECIAL_ORDER` | Orderable only via the sales department. |
| `OVERSIZED` | Large package; delivery restrictions apply. |
| `PRODUCT_BLOCKED` | Blocked for sale. |

### Units

TME returns unit ids (`ST` = pcs, `V`, `A`, `W`, `OHM`, `F`, `HZ`, `MM`, …) on
`unit`, `weight.unit` and similar fields. Their full ~280-row table is **deliberately
not reproduced here**: it exists only as rendered HTML in their docs, several rows are
malformed at the source, and every mechanical extraction produced a table that drifted
out of alignment part-way through. A silently misaligned unit table would be worse
than none. If a future action needs the full mapping, read it from the "Units" table
on TME's own documentation page (api-doc.tme.eu/v2, under Glossaries) rather than
trusting a transcription.

---

# Endpoints

Generated from the specification. Field trees are fenced blocks rather than nested
lists — these schemas reach ten levels, and deep two-space nesting is read
inconsistently across Markdown renderers. Example payloads are collapsed.

### `POST /auth/token`

**Parameters**

_No parameters._

**Request body** (`application/x-www-form-urlencoded`)

```text
— variant 1 —
  grant_type     enum(client_credentials)   — Type of authorization flow.
— variant 2 —
  grant_type     enum(refresh_token)   — Type of authorization flow.
  refresh_token  string   — Refresh token.
```

**Response**

```text
access_token   string   — Token for authorizing API requests.
token_type     string   — Type of the returned token.
expires_in     number   — Token expiry time in seconds.
refresh_token  string   — Token for obtaining a new access token.
```

<details><summary>Example response</summary>

```json
{
  "access_token": "",
  "token_type": "Bearer",
  "expires_in": 300,
  "refresh_token": ""
}
```
</details>

### `GET /products`

**Note:** Provide either `symbols` or `mpns`.

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `symbols[]` | array<string> (1..50 items) | **yes** | List of product symbols. Example: `["M7-DIO", "AX-100"]` |
| `mpns[]` | array<string> (1..50 items) | **yes** | List of product MPNs. Example: `["A76-U10", "SKU 111"]` |

**Response**

```text
status                      string   — Response status. "OK" indicates that the action was successful.
data                        object   — Action response data.
  elements                  array<object>
    product_status          array<string>   — List of product statuses.
    symbol                  string   — Unique product identifier.
    ean                     string   — EAN number (barcode). Can be empty.
    customer_symbol         string   — Customer symbol for the product, if provided.
    category                object   — Product category details.
      id                    number   — Category identifier.
      name                  string   — Category name.
    manufacturer_symbols    array<string>   — List of manufacturer symbols for the product.
    manufacturer            object   — Product manufacturer details.
      id                    number   — Manufacturer identifier.
      name                  string   — Manufacturer name.
    description             string   — Product description.
    multiples               number   — Product multiplicity. Product quantity must be a multiple of this value.
    minimal_amount          number   — Minimal order quantity.
    weight                  object   — Product weight details.
      value                 number   — Product weight.
      unit                  string   — Unit in which the weight is provided.
    unit                    object   — Product unit details.
      id                    string   — Unit identifier.
      short_name            string   — Short name of unit type e.g. 'pcs'.
      singular_translation  string|null   — Name of unit e.g. 'Piece'.
      plural_translation    string|null   — Plural translation e.g. 'Pieces'.
    packing                 object   — List of available product packaging options.
      elements              array<object>
        id                  string   — Packaging type identifier.
        translation         string   — Packaging type name.
        amount              number   — Number of items in package.
    assets                  object   — Product image assets.
      primary_photo         object|null   — Primary image with different resolutions.
        prime               string   — Main image URL.
        thumbnail           string   — Thumbnail image URL.
        high_resolution     string|null   — High-resolution image URL.
    notification            object|null   — Product availability notification settings.
      any_increase          boolean   — Indicates if notifications are triggered on any stock increase up to the `required_amount`.
      created_at            string   — Timestamp of the notification creation.
      required_amount       number   — Required product quantity.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "product_status": [
          "AVAILABLE_WHILE_STOCKS_LAST"
        ],
        "symbol": "AX-100",
        "ean": "",
        "customer_symbol": "",
        "category": {
          "id": 112610,
          "name": "Portable digital multimeters"
        },
        "manufacturer_symbols": [],
        "manufacturer": {
          "id": 271,
          "name": "AXIOMET / Transfer Multisort Elektronik Sp. z o.o."
        },
        "description": "Digital multimeter; LCD 3,5 digit (1999); 3x/s; 0÷40°C",
        "multiples": 1,
        "minimal_amount": 1,
        "weight": {
          "value": 239.4,
          "unit": "g"
        },
        "unit": {
          "id": "ST",
          "short_name": "pcs",
          "singular_translation": "Piece",
          "plural_translation": null
        },
        "packing": {
          "elements": [
            {
              "id": "CT2",
              "translation": "Cardboard",
              "amount": 2
            }
          ]
        },
        "assets": {
          "primary_photo": {
            "prime": "//ce8dc832c.cloudimg.io/v7/_cdn_/A7/25/90/00/0/610938_1.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=076bdcd2064eab934dbc198feae0f6966b8b46b2",
            "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn_/A7/25/90/00/0/610938_1.jpg?width=100&height=75&q=75&ci_sign=085b9ae47f022f053d0a471a419c1610c84d8d6d",
            "high_resolution": "//ce8dc832c.cloudimg.io/v7/_cdn_/A7/25/90/00/0/610938_1.jpg?width=1440&height=1080&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=ec32cbb3f2144578ae9262a384200194e5e33558"
          }
        },
        "notification": {
          "any_increase": false,
          "created_at": "2026-05-07T11:36:07+02:00",
          "required_amount": 800
        }
      }
    ]
  }
}
```
</details>

### `GET /products/parameters`

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `symbols[]` | array<string> (1..50 items) | **yes** | List of product symbols. Example: `["M7-DIO", "AX-100"]` |

**Response**

```text
status           string   — Response status. "OK" indicates that the action was successful.
data             object   — Action response data.
  elements       array<object>
    symbol       string   — Product symbol.
    parameters   object   — List of product parameters.
      elements   array<object>
        id       number   — Parameter identifier.
        name     string   — Parameter name.
        values   array<object>   — List of parameter values.
          id     number   — Value identifier.
          value  string   — Value label.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "symbol": "W10R-4A",
        "parameters": {
          "elements": [
            {
              "id": 2,
              "name": "Manufacturer",
              "values": [
                {
                  "id": 144,
                  "value": "Zakłady Podzespołów Radiowych MIFLEX SA"
                }
              ]
            },
            {
              "id": 149,
              "name": "Type of generator",
              "values": [
                {
                  "id": 1518625,
                  "value": "high voltage"
                }
              ]
            },
            {
              "id": 120,
              "name": "Operating voltage",
              "values": [
                {
                  "id": 1449928,
                  "value": "230V AC"
                }
              ]
            },
            {
              "id": 63,
              "name": "Number of outputs",
              "values": [
                {
                  "id": 1454201,
                  "value": "4"
                }
              ]
            },
            {
              "id": 32,
              "name": "Operating temperature",
              "values": [
                {
                  "id": 1820509,
                  "value": "0...130°C"
                }
              ]
            },
            {
              "id": 157,
              "name": "Electrical life",
              "values": [
                {
                  "id": 1518623,
                  "value": "4000000 cycles"
                }
              ]
            },
            {
              "id": 364,
              "name": "Output voltage",
              "values": [
                {
                  "id": 1602215,
                  "value": "20kV"
                }
              ]
            }
          ]
        }
      }
    ]
  }
}
```
</details>

### `GET /products/files`

This action returns a list of additional photos and documents correlated with specified products. The method is limited by the maximum number of product symbols that can be submitted as an input to this action. The maximum number of symbols is equal to 50.

Endpoint can return these types of documents:

- **INS** - Manual,
- **DTE** - Documentation,
- **KCH** - Safety Data Sheet,
- **GWA** - Warranty,
- **INB** - Safety instructions,
- **MOV** - Video,
- **YTB** - YouTube video,
- **PRE** - Presentation,
- **SFT** - Software.

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `symbols[]` | array<string> (1..50 items) | **yes** | List of product symbols. Example: `["M7-DIO", "AX-100"]` |

**Response**

```text
status                     string   — Response status. "OK" indicates that the action was successful.
data                       object   — Action response data.
  elements                 array<object>
    symbol                 string   — Product symbol.
    assets                 object   — Product image assets.
      primary_photo        object|null   — Primary image with different resolutions.
        prime              string   — Main image URL.
        thumbnail          string   — Thumbnail image URL.
        high_resolution    string|null   — High-resolution image URL.
      additional           object   — List of additional images with different resolutions.
        elements           array<object>
          prime            string   — Main image URL.
          thumbnail        string   — Thumbnail image URL.
          high_resolution  string|null   — High-resolution image URL.
      presentation         object   — List of images used for 360° product presentation.
        elements           array<object>
          photo            string   — Image URL.
          position         number   — Image position number.
    documents              object   — List of product documents.
      elements             array<object>
        url                string   — Document URL.
        type               string   — Document type.
        size               integer   — Document size.
        file_name          string   — File name.
        language           string   — Document language identifier.
    parameter_images       object
      elements             array<object>
        name               string   — Image name.
        url                string   — Image URL.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "symbol": "ARG-15391",
        "assets": {
          "primary_photo": {
            "prime": "//ce8dc832c.cloudimg.io/v7/_cdn_/2C/F0/C0/00/0/790466_1.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=43bf7a99130ffc42ba3c75ee8bf9a6da7ed276e3",
            "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn_/2C/F0/C0/00/0/790466_1.jpg?width=100&height=75&q=75&ci_sign=8f71bfcc8b7ab2a52af5217399936383d1eb42aa",
            "high_resolution": "//ce8dc832c.cloudimg.io/v7/_cdn_/2C/F0/C0/00/0/790466_1.jpg?width=1440&height=1080&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=37e148a4e696422086281fa455852719bd7b5955"
          },
          "additional": {
            "elements": [
              {
                "prime": "//ce8dc832c.cloudimg.io/v7/_cdn_/6F/F0/C0/00/0/790518_1.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=0aea75a89c86ead4680674fa0cf63dfb3fa65aad",
                "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn_/6F/F0/C0/00/0/790518_1.jpg?width=100&height=75&q=75&ci_sign=6d3df80b2706044723e04cf7bddd5e1ebd00dbd7",
                "high_resolution": "//ce8dc832c.cloudimg.io/v7/_cdn_/6F/F0/C0/00/0/790518_1.jpg?width=1440&height=1080&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=08bbf99bc9ee78bb0580bfffa410d2a236e4f04a"
              }
            ]
          },
          "presentation": {
            "elements": [
              {
                "photo": "//ce8dc832c.cloudimg.io/v7/_cdn_/4A/E0/C0/00/0/790180_1.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=2df1b3b5050823e37ba0e331d628c78c1807c7b5",
                "position": 0
              },
              {
                "photo": "//ce8dc832c.cloudimg.io/v7/_cdn_/4A/E0/C0/00/0/790180_2.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=c489b0c5cbc99f8684e853d6a007bbe4bba1ff8d",
                "position": 1
              },
              {
                "photo": "//ce8dc832c.cloudimg.io/v7/_cdn_/4A/E0/C0/00/0/790180_3.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=7698fbe21fd5dfd49294cce35b670eab9a130381",
                "position": 2
              },
              {
                "photo": "//ce8dc832c.cloudimg.io/v7/_cdn_/4A/E0/C0/00/0/790180_4.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=44b71a8fc6942c1d2d7bc99eef78215ec448be22",
                "position": 3
              },

  … (truncated)
```
</details>

### `GET /products/search`

**Note:** Provide either `phrase` or `category_id`, or both.

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `scope[]` | array<enum(products, parameters, counters)> | **yes** | Type of data to be returned. At least one value is required. Example: `["products", "counters"]` |
| `phrase` | string | **yes** | Search phrase, which may contain multiple words. Example: `"led diode"` |
| `category_id` | number | **yes** |  |
| `manufacturer_id` | number | no | Manufacturer identifier. |
| `limit` | number | no |  |
| `page` | number | no |  |
| `assortment_type` | enum(internal, external) | no |  |
| `customer_symbol_filter` | boolean | no |  |
| `filter` | object | no |  |
| `sort` | object | no |  |
| `parameters[]` | array<object> | no | Filter parameters grouped by index. Each group must contain `parameters[n][id]` and one or more `parameters[n][values][]`. Multiple filter groups can be specified. Example: `parameters[0][id]=2&parameters[0][values][]=156&parameters[0][values][]=179&parameters[1][id]=367&parameters[1][values][]=1443865` |

**Response**

```text
status                        string   — Response status. "OK" indicates that the action was successful.
data                          object   — Action response data.
  products                    object|null   — List of products.
    elements                  array<object>
      product_status          array<string>   — List of product statuses.
      symbol                  string   — Unique product identifier.
      ean                     string   — EAN number (barcode). Can be empty.
      customer_symbol         string   — Customer symbol for the product, if provided.
      category                object   — Product category details.
        id                    number   — Category identifier.
        name                  string   — Category name.
      manufacturer_symbols    array<string>   — List of manufacturer symbols for the product.
      manufacturer            object   — Product manufacturer details.
        id                    number   — Manufacturer identifier.
        name                  string   — Manufacturer name.
      description             string   — Product description.
      multiples               number   — Product multiplicity. Product quantity must be a multiple of this value.
      minimal_amount          number   — Minimal order quantity.
      weight                  object   — Product weight details.
        value                 number   — Product weight.
        unit                  string   — Unit in which the weight is provided.
      unit                    object   — Product unit details.
        id                    string   — Unit identifier.
        short_name            string   — Short name of unit type e.g. 'pcs'.
        singular_translation  string|null   — Name of unit e.g. 'Piece'.
        plural_translation    string|null   — Plural translation e.g. 'Pieces'.
      packing                 object   — List of available product packaging options.
        elements              array<object>
          id                  string   — Packaging type identifier.
          translation         string   — Packaging type name.
          amount              number   — Number of items in package.
      assets                  object   — Product image assets.
        primary_photo         object|null   — Primary image with different resolutions.
          prime               string   — Main image URL.
          thumbnail           string   — Thumbnail image URL.
          high_resolution     string|null   — High-resolution image URL.
      notification            object|null   — Product availability notification settings.
        any_increase          boolean   — Indicates if notifications are triggered on any stock increase up to the `required_amount`.
        created_at            string   — Timestamp of the notification creation.
        required_amount       number   — Required product quantity.
  parameters                  object|null   — List of related filter parameters.
    elements                  array<object>
      id                      number   — Parameter identifier.
      name                    string   — Parameter name.
      values                  array<object>   — List of parameter values.
        id                    number   — Value identifier.
        value                 string   — Value label.
        products_count        number   — Number of products with this value.
        selected              boolean   — Indicates if the value is selected.
        is_active             boolean   — Indicates if the value is active.
      products_count          number   — Number of products with this parameter.
  counters                    object|null   — Pagination info and category counts.
    pages                     number   — Total number of pages.
    count                     number   — Total number of products.
    page                      number   — Current page number.
    categories                object   — List of categories for returned products.
      elements                array<object>
        id                    number   — Category identifier.
        products_count        number   — Number of products in the category.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "products": {
      "elements": [
        {
          "product_status": [
            "HARDLY_AVAILABLE",
            "PROMOTED"
          ],
          "symbol": "AX-1000",
          "ean": "",
          "customer_symbol": "",
          "category": {
            "id": 112516,
            "name": "Current Transformers"
          },
          "manufacturer_symbols": [
            "AX-1000"
          ],
          "manufacturer": {
            "id": 346,
            "name": "TALEMA / NT Magnetics s.r.o."
          },
          "description": "Current transformer; AX; Iin: 10A; 100Ω; -40÷120°C; Trans: 1000: 1",
          "multiples": 1,
          "minimal_amount": 1,
          "weight": {
            "value": 7.55,
            "unit": "g"
          },
          "unit": {
            "id": "ST",
            "short_name": "pcs",
            "singular_translation": "Piece",
            "plural_translation": null
          },
          "packing": {
            "elements": []
          },
          "assets": {
            "primary_photo": {
              "prime": "//ce8dc832c.cloudimg.io/v7/_cdn_/B8/DB/50/00/0/376203_1.jpg?width=640&height=480&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=b838c94eb9255157e0945a28ec732239bd57ebbf",
              "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn_/B8/DB/50/00/0/376203_1.jpg?width=100&height=75&q=75&ci_sign=124fff2e1f2563df945535f01c37a04cc9a026ba",
              "high_resolution": "//ce8dc832c.cloudimg.io/v7/_cdn_/B8/DB/50/00/0/376203_1.jpg?width=1440&height=1080&wat=1&wat_url=_tme-wrk_%2Ftme_new.png&wat_scale=100p&ci_sign=2256d20e29ef14b04066f5b5eeafd74596f9d95a"
            }
          },
          "notification": {
            "any_increase": false,
            "created_at": "2026-05-05T10:01:31+02:00",
            "required_amount": 500
          }
        },
        {
          "product_status": [],
          "symbol": "CT-MAX-1000",
          "ean": "",
          "customer_symbol": "",
          "category": {
            "id": 118107,
            "name": "Current Transformers"
          },
          "manufacturer_symbols": [
            "2CSG225995R1101"
          ],
          "manufacturer": {
            "id": 689,
            "name": "ABB Sp. z o.o."
          },
          "description": "Current transformer; Iin: 1kA; Iout: 5A; 0.5@max10VA; Øint: 30mm",
          "multiples": 1,
          "minimal_amount": 1,
          "weight": {
            "value": 300,
            "unit": "g"
          },
          "unit": {
            "id": "ST",
            "short_name":
  … (truncated)
```
</details>

### `GET /products/data`

This action returns a list of prices, stock levels and deliveries of products to TME warehouse details. This method is limited by the maximum number of symbols that can be submitted as input to this action. The maximum number of symbols is equal to 50.

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `currency` | string | no | Currency identifier, according to which price values will be returned. Example: `"EUR"` |
| `scope[]` | array<enum(prices, stock, delivery, delivery_confirmed)> | **yes** | Type of data to be returned. At least one value is required. - If `scope[]` includes `prices` or `stock`, only `symbols[]` is required. - If `scope[]` includes `delivery` or `delivery_confirmed`, both `symbols[]` and `amounts[]` are required. - `delivery` and `delivery_confirmed` cannot be used together. Example: `["prices", "delivery"]` |
| `symbols[]` | array<string> (1..50 items) | **yes** | List of product symbols. Example: `["M7-DIO", "AX-100"]` |
| `amounts[]` | array<number> (1..50 items) | **yes** | A list of product quantities used to calculate delivery availability and confirmed delivery dates. This parameter is mandatory when requesting data within the `delivery` or `delivery_confirmed` scope. Delivery information will be returned for each quantity specified in the request. List of amounts for products given in the `symbols[]` parameter. The order of elements in `amounts[]` must match the order of elements in `symbols[]`. Example: `["100", "2"]` |

**Response**

```text
status                      string   — Response status. "OK" indicates that the action was successful.
data                        object   — Action response data.
  elements                  array<object>
    stock_quantity          number|null   — Number of products in stock. Returns null if only `prices` query parameter is selected.
    symbol                  string   — Unique product identifier.
    unit                    object   — Product unit details.
      id                    string   — Unit identifier.
      short_name            string   — Short name of unit type e.g. 'pcs'.
      singular_translation  string|null   — Name of unit e.g. 'Piece'.
      plural_translation    string|null   — Plural translation e.g. 'Pieces'.
    prices                  object|null   — Product pricing details. Returns null if `prices` query parameter is not selected.
      elements              array<object>   — List of quantity-based price tiers.
      tax                   object   — Product tax details.
        type                string   — Tax type.
        rate                string   — Tax percentage rate.
      currency              string   — Currency of returned prices.
      type                  enum(NET, GROSS)   — Product price type.
    deliveries              object|null   — List of product stock and delivery status details. Returns null if `delivery` or `delivery_confirmed` query parameter is not selected.
      elements              array<object>
        status              string   — Information about availability: - **DS_AVAILABLE_IN_STOCK** - Product available in stock. - **DS_DATE_AS_WEEK** - Product ordered from supplier with confirmed delivery. - **DS_SUPPLIER_WAREHOUSE_DATE_AS_WEEK** - Product available from supplier. Minimum purchase quantities may apply. Estimated warehouse delivery is provided (for an order placed today). - **DS_WAITING_FOR_CONFIRMATION_FROM_VENDOR** - Product ordered from supplier, awaiting confirmation. - **DS_DELIVERY_NEEDS_CONFIRMATION** - Product not yet ordered from supplier. Please contact our sales department. For **DS_DATE_AS_WEEK** and **DS_SUPPLIER_WAREHOUSE_DATE_AS_WEEK** statuses, `data` returns `week`, `year`, `range_start`, `range_end` and `supply_date`. For **DS_DELIVERY_NEEDS_CONFIRMATION** status, `data` returns `waiting_period` and `supply_date`. For all other statuses, `data` is null.
        amount              number   — Number of items available for the status.
        data                object|null   — Estimated delivery date details based on the status.
          week              string   — ISO delivery week.
          year              string   — Delivery year.
          range_start       string   — Start of the delivery week.
          range_end         string   — End of the delivery week.
          waiting_period    string   — Standard manufacturer lead time in weeks.
          supply_date       string   — Estimated delivery date.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "stock_quantity": 647,
        "symbol": "02KR-6H-P",
        "unit": {
          "id": "ST",
          "short_name": "pcs",
          "singular_translation": "Piece",
          "plural_translation": null
        },
        "prices": {
          "elements": [
            {
              "amount": 1,
              "price": 0.647,
              "special": false
            },
            {
              "amount": 2000,
              "price": 0.6052,
              "special": false
            }
          ],
          "tax": {
            "type": "VAT",
            "rate": 23
          },
          "currency": "PLN",
          "type": "GROSS"
        },
        "deliveries": {
          "elements": [
            {
              "status": "DS_AVAILABLE_IN_STOCK",
              "amount": 647,
              "data": null
            },
            {
              "status": "DS_DELIVERY_NEEDS_CONFIRMATION",
              "amount": 353,
              "data": {
                "waiting_period": "P5W",
                "supply_date": "2026-06-17"
              }
            }
          ]
        }
      }
    ]
  }
}
```
</details>

### `GET /products/symbols`

The endpoint returns symbols for products active in TME.

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `category_id` | number | no |  |
| `manufacturer_id` | number | no | Manufacturer identifier. |
| `paginate` | object | no |  |

**Response**

```text
status      string   — Response status. "OK" indicates that the action was successful.
data        object   — Action response data.
  elements  array<string>   — List of product symbols.
  pages     number   — Total number of pages.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      "EX380976-K",
      "MX655",
      "CM275",
      "CM275-NIST",
      "CA-F405",
      "CA-F603",
      "CA-F605",
      "PKT-P1670",
      "TA167",
      "UT216C",
      "UT207",
      "UT210E",
      "UT213C",
      "UT221",
      "UT222",
      "UT256B",
      "MA1500",
      "KEW2510",
      "BM035",
      "BM037"
    ],
    "pages": 9
  }
}
```
</details>

### `GET /products/related`

This endpoint returns products related to the specified product symbol. Related products may include compatible accessories and complementary items. For example, for a battery product, the response may include compatible chargers. Consider that not all of the TME symbols have related products.

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `symbol` | string | **yes** | Product symbol. |
| `manufacturer_id` | number | no | Manufacturer identifier. |

**Response**

```text
status      string   — Response status. "OK" indicates that the action was successful.
data        object   — Action response data.
  elements  array<string>   — Symbols array of related products.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      "ACCU-R22/175-EG",
      "ACCU-6F22/GP-RE",
      "ACCU-R22/170-GP"
    ]
  }
}
```
</details>

### `GET /products/similar`

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `symbol` | string | **yes** | Product symbol. |
| `manufacturer_id` | number | no | Manufacturer identifier. |

**Response**

```text
status      string   — Response status. "OK" indicates that the action was successful.
data        object   — Action response data.
  elements  array<string>   — Symbols array of similar products.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      "15XP-B",
      "30XR-A",
      "33XR-A"
    ]
  }
}
```
</details>

### `GET /products/categories/list`

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `root_category_id` | number | no | Top-level category identifier. |
| `manufacturer_id` | number | no | Manufacturer identifier. |

**Response**

```text
status              string   — Response status. "OK" indicates that the action was successful.
data                object   — Action response data.
  elements          array<object>
    parent_id       number   — Parent category identifier.
    id              number   — Category identifier.
    products_count  number   — Number of products in the category.
    thumbnail       string   — Category thumbnail image URL.
    name            string   — Category name.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "parent_id": 100164,
        "id": 100277,
        "products_count": 120,
        "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/160010090.png?width=170&height=128&ci_sign=d0f8e1044ba569ce7e5243bcda5de6057b8ca762",
        "name": "Laboratory instruments"
      },
      {
        "parent_id": 100277,
        "id": 112658,
        "products_count": 58,
        "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/160010090060.png?width=170&height=128&ci_sign=b5d8503d5f4bbeaf8b615b450e4ba2ab62bce7b9",
        "name": "Calibrators"
      },
      {
        "parent_id": 100277,
        "id": 112659,
        "products_count": 62,
        "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/160010090070.png?width=170&height=128&ci_sign=2a43806f8ca5d986dd20050168883b8b2a4df2a5",
        "name": "Calibrators - Accessories"
      }
    ]
  }
}
```
</details>

### `GET /products/categories/tree`

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `root_category_id` | number | no | Top-level category identifier. |
| `manufacturer_id` | number | no | Manufacturer identifier. |

**Response**

```text
status      string   — Response status. "OK" indicates that the action was successful.
data        object   — Action response data.
  elements  object
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": {
      "parent_id": 111000,
      "id": 63,
      "products_count": 436,
      "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070.png?width=170&height=128&ci_sign=3ea9609c76bc8739b8e21e463e128c601f006732",
      "name": "Sound Sources",
      "children": [
        {
          "parent_id": 63,
          "id": 100207,
          "products_count": 197,
          "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070010.png?width=170&height=128&ci_sign=6907243fad9e64f1483cd3d296ddb0d10627b9c8",
          "name": "Speakers",
          "children": []
        },
        {
          "parent_id": 63,
          "id": 100225,
          "products_count": 16,
          "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070020.png?width=170&height=128&ci_sign=49e8d0ebcb21d3ce8bc3ec95214120e42a9ca4eb",
          "name": "Microphones and Headsets",
          "children": []
        },
        {
          "parent_id": 63,
          "id": 100208,
          "products_count": 223,
          "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070030.png?width=170&height=128&ci_sign=16809dca23417f933c9b9608aa97950e91d6f966",
          "name": "Sounders",
          "children": [
            {
              "parent_id": 100208,
              "id": 112496,
              "products_count": 97,
              "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070030010.png?width=170&height=128&ci_sign=2680167689dd00b629297469d9d9fb1edbfc42b2",
              "name": "Electromagnetic Sounders",
              "children": [
                {
                  "parent_id": 112496,
                  "id": 112497,
                  "products_count": 36,
                  "children": [],
                  "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070030010010.png?width=170&height=128&ci_sign=5cad7aa19f244382e394f8adf6db196b341f9027",
                  "name": "Electromagnetic Sounders with Generator"
                },
                {
                  "parent_id": 112496,
                  "id": 112498,
                  "products_count": 61,
                  "children": [],
                  "thumbnail": "//ce8dc832c.cloudimg.io/v7/_cdn-category_/070030010020.png?width=170&height=128&ci_sign=617f9b0200c0cb69cf95f72f1a7bb1109d6beca9",
                  "name": "Electromagnetic Sounders w/o Generator"
                }
              ]
            },
            {
              "parent_id": 100208,
              "id": 112499,
              "products_count": 101,
              "thumbnail": "//ce8dc832c
  … (truncated)
```
</details>

### `GET /products/manufacturers`

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |
| `category_id` | number | no |  |
| `sort[field]` | enum(name, items_count) | no | Field name to use for sorting results. |

**Response**

```text
status              string   — Response status. "OK" indicates that the action was successful.
data                object   — Action response data.
  elements          array<object>
    id              number   — Manufacturer identifier.
    name            string   — Manufacturer name.
    products_count  number   — Number of products by manufacturer.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "id": 1418,
        "name": "Aptiv",
        "products_count": 1
      },
      {
        "id": 317,
        "name": "BeStar Holding Co., LTD",
        "products_count": 42
      },
      {
        "id": 1464,
        "name": "Cre-sound Electronics",
        "products_count": 34
      },
      {
        "id": 321,
        "name": "DIGISOUND",
        "products_count": 7
      },
      {
        "id": 323,
        "name": "LOUDITY / Transfer Multisort Elektronik Sp. z o.o.",
        "products_count": 93
      },
      {
        "id": 1356,
        "name": "MPM",
        "products_count": 27
      },
      {
        "id": 828,
        "name": "ONPOW Push Button Manufacture Co.,Ltd.",
        "products_count": 24
      },
      {
        "id": 322,
        "name": "VISATON",
        "products_count": 182
      }
    ]
  }
}
```
</details>

### `GET /utils/countries`

**Parameters**

_No parameters._

**Response**

```text
status      string   — Response status. "OK" indicates that the action was successful.
data        object   — Action response data.
  elements  array<object>
    id      string   — Country identifier.
    name    string   — Country name.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "elements": [
      {
        "id": "AD",
        "name": "Andorra"
      },
      {
        "id": "AE",
        "name": "United Arab Emirates"
      },
      {
        "id": "AG",
        "name": "Antigua and Barbuda"
      },
      {
        "id": "AI",
        "name": "Anguilla"
      },
      {
        "id": "AL",
        "name": "Albania"
      },
      {
        "id": "AM",
        "name": "Armenia"
      }
    ]
  }
}
```
</details>

### `GET /utils/currencies`

**Parameters**

| Parameter | Type | Required | Description |
| --- | --- | --- | --- |
| `country` | string | no | Country identifier. Example: `"GB"` |

**Response**

```text
status        string   — Response status. "OK" indicates that the action was successful.
data          object   — Action response data.
  currencies  array<string>   — List of available currencies.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "currencies": [
      "PLN",
      "EUR",
      "GBP",
      "USD"
    ]
  }
}
```
</details>

### `GET /utils/languages`

**Parameters**

_No parameters._

**Response**

```text
status       string   — Response status. "OK" indicates that the action was successful.
data         object   — Action response data.
  languages  array<string>   — List of language codes.
```

<details><summary>Example response</summary>

```json
{
  "status": "OK",
  "data": {
    "languages": [
      "bg",
      "cs",
      "da",
      "de",
      "el",
      "en",
      "es",
      "fi",
      "fr",
      "hu",
      "it",
      "ja",
      "ko",
      "nl",
      "pl",
      "pt",
      "ro",
      "ru",
      "sk",
      "sv",
      "tr",
      "uk",
      "vi",
      "zh"
    ]
  }
}
```
</details>
