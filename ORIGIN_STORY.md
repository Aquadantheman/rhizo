# Why I Built UDR

I was building a real estate analytics platform called Lotitude - aggregating 50+ public data sources into a unified scoring system for 856,000 NYC parcels. Every night, I'd refresh 77 data compounds, rebuild a master table, and push it to production.

And every night, I'd hold my breath.

If the pipeline crashed halfway through, I'd lose data. If I wanted to know "what changed since yesterday," I had no answer. If I wanted to test a new scoring algorithm, I had to copy the entire dataset. I was spending more time managing infrastructure than building the actual product.

I looked at Delta Lake, Iceberg, Hudi. They solved single-table versioning, but I needed cross-table transactions. I looked at Kafka for change tracking, but that meant running another system. I looked at Git LFS for versioning, but I needed to query the data, not just store it.

Nothing unified it all.

So I built UDR.

Content-addressable storage for automatic deduplication. Cross-table ACID transactions. Git-like branching for data. Time travel queries. Change tracking without Kafka. One system instead of five.

Now my nightly pipeline is atomic - it either fully succeeds or fully rolls back. I can query any historical version of any parcel. I can branch to test new scoring weights and merge when I'm happy. My storage costs dropped 90% because identical chunks are stored once.

I built this for myself. But the more I used it, the more I realized: every data team has this problem. The fragmentation isn't a Lotitude problem - it's an industry problem.

Linus Torvalds built Linux in 1991. Fourteen years later, he got frustrated with the source control tools available for managing Linux development, so he built Git. Git became arguably more influential than Linux itself.

I'm not comparing myself to Linus. But the pattern resonates: Lotitude is my Linux - the thing I actually wanted to build. UDR is my Git - the infrastructure I built because nothing else worked.

Lotitude stays proprietary. The scoring algorithms, the domain expertise - that's my edge.

But UDR? That's just infrastructure. It shouldn't be proprietary. Every data team deserves these primitives.

So I'm open-sourcing it.

---

**UDR is available now at [github.com/...](#)**

If you've ever held your breath during a data pipeline run, I built this for you.
