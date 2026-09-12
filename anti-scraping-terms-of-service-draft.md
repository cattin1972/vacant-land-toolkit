# Anti-Scraping / Acceptable Use — Terms of Service Draft

**IMPORTANT: this is a starting draft, not final legal language.** Before this
goes live on a real website used by real paying customers, have an actual
lawyer review it — the wording below is a reasonable, informed starting
point (modeled on how legitimate real-estate-data companies phrase this),
not a substitute for legal advice. Contract enforceability, especially
around automated-access clauses, varies by state and depends on wording
precision that matters a lot in an actual dispute.

---

## Suggested clause: Acceptable Use / No Automated Access

> **Prohibited Conduct.** You may not, and may not permit any third party
> to:
>
> (a) access, query, or extract data from the Service using any robot,
> spider, scraper, crawler, or other automated means, except through the
> Service's officially published API (if any) and strictly within the
> rate limits and usage terms of your account tier;
>
> (b) circumvent, disable, or otherwise interfere with rate limiting,
> access controls, or other security-related features of the Service;
>
> (c) reproduce, duplicate, copy, sell, resell, sublicense, or otherwise
> exploit any portion of the Service's data, in whole or in part, for any
> purpose without our prior written consent;
>
> (d) use the Service to build, train, or populate a competing product or
> database;
>
> (e) share your account credentials or API key with any person or entity
> not authorized on your account, or use another user's credentials.
>
> We may monitor use of the Service to detect violations of this section,
> including through automated means. Violation of this section is grounds
> for immediate suspension or termination of your account without refund,
> and we reserve the right to pursue all available legal remedies.

## Suggested companion clause: Rate Limits & Fair Use

> Your account is subject to usage limits based on your subscription
> tier, as described at [pricing page URL]. We reserve the right to
> throttle, delay, or block requests that exceed these limits or that we
> reasonably believe are automated in a manner inconsistent with normal
> individual use of the Service.

## Suggested companion clause: No Warranty on Data Accuracy

(Worth including regardless of anti-scraping concerns — protects you
given how much of this toolkit's output is explicitly "best-effort" or
"not a guaranteed answer," per the honesty caveats built throughout the
underlying script.)

> Data provided through the Service is aggregated from third-party and
> public sources and is provided "as is." We do not guarantee its
> accuracy, completeness, or timeliness. The Service is a research tool
> and does not constitute legal, financial, engineering, or professional
> advice. Always independently verify critical information (including
> but not limited to flood zone status, tax/lien status, zoning, and
> utility availability) before making a purchase decision.

---

## Non-legal technical checklist (for whenever the website/API gets built)

- [ ] No route serves real data to an unauthenticated request
- [ ] Per-account rate limiting (requests/minute AND records/day)
- [ ] Bot-detection/WAF service in front of the site (Cloudflare Bot
      Management is a reasonable, well-known starting option; DataDome
      is a pricier, more specialized alternative — same category
      Propwire uses)
- [ ] A small number of intentionally-fake "trap" records seeded into
      the dataset, tied to real-looking but nonexistent parcels, so a
      wholesale copy of the data would be detectable
- [ ] Logging + alerting on anomalous account activity (usage spikes,
      sequential ID enumeration, requests with no normal browser
      headers)
- [ ] robots.txt that explicitly disallows crawling of data/search
      routes (weak on its own, but easy, and removes any "we didn't say
      you couldn't" argument)
- [ ] CAPTCHA or equivalent challenge triggered on suspicious patterns,
      not shown to normal users by default
