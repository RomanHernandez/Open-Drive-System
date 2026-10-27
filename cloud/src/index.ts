import { env } from "cloudflare:workers";

interface FileRow {
	account: string;
	id: string;
	name: string;
	mime: string;
	size: number | null;
	md5: string | null;
	created: string | null;
	modified: string | null;
	trashed: number;
	starred: number;
	shared: number;
	parent: string | null;
	owner: string | null;
	link: string | null;
}

const SCHEMA_STMTS = [
	`CREATE TABLE IF NOT EXISTS files (
		account TEXT, id TEXT, name TEXT, mime TEXT, size INTEGER, md5 TEXT,
		created TEXT, modified TEXT, trashed INTEGER, starred INTEGER, shared INTEGER,
		parent TEXT, owner TEXT, link TEXT, seen_run INTEGER, deleted INTEGER DEFAULT 0,
		PRIMARY KEY(account, id)
	)`,
	`CREATE INDEX IF NOT EXISTS idx_md5 ON files(md5)`,
	`CREATE INDEX IF NOT EXISTS idx_name ON files(name)`,
	`CREATE TABLE IF NOT EXISTS runs (
		run INTEGER PRIMARY KEY AUTOINCREMENT, account TEXT, started TEXT,
		finished TEXT, files INTEGER
	)`,
];

async function ensureSchema() {
	await env.DB.batch(SCHEMA_STMTS.map((s) => env.DB.prepare(s)));
}

async function timingSafeEqual(a: string, b: string): Promise<boolean> {
	const enc = new TextEncoder();
	const ha = await crypto.subtle.digest("SHA-256", enc.encode(a));
	const hb = await crypto.subtle.digest("SHA-256", enc.encode(b));
	const va = new Uint8Array(ha);
	const vb = new Uint8Array(hb);
	let diff = 0;
	for (let i = 0; i < va.length; i++) diff |= va[i] ^ vb[i];
	return diff === 0;
}

async function getAccessToken(refreshToken: string): Promise<string> {
	const body = new URLSearchParams({
		client_id: env.GOOGLE_CLIENT_ID,
		client_secret: env.GOOGLE_CLIENT_SECRET,
		refresh_token: refreshToken,
		grant_type: "refresh_token",
	});
	const res = await fetch("https://oauth2.googleapis.com/token", {
		method: "POST",
		headers: { "Content-Type": "application/x-www-form-urlencoded" },
		body,
	});
	if (!res.ok) {
		throw new Error(`OAuth token error (${res.status})`);
	}
	const data = (await res.json()) as { access_token: string };
	return data.access_token;
}

const DRIVE_FIELDS =
	"nextPageToken,files(id,name,mimeType,size,md5Checksum,createdTime,modifiedTime,trashed,starred,shared,parents,owners(emailAddress),webViewLink)";

async function syncAccount(account: string, refreshToken: string): Promise<number> {
	await ensureSchema();
	const token = await getAccessToken(refreshToken);
	const started = new Date().toISOString().slice(0, 19).replace("T", " ");
	const runRes = await env.DB.prepare("INSERT INTO runs(account, started) VALUES(?, ?)").bind(account, started).run();
	const runId = Number(runRes.meta.last_row_id || Date.now());

	let pageToken = "";
	let total = 0;

	while (true) {
		const params = new URLSearchParams({
			pageSize: "1000",
			fields: DRIVE_FIELDS,
			q: "'me' in owners or sharedWithMe = true",
			includeItemsFromAllDrives: "false",
		});
		if (pageToken) params.set("pageToken", pageToken);

		const res = await fetch(`https://www.googleapis.com/drive/v3/files?${params.toString()}`, {
			headers: { Authorization: `Bearer ${token}` },
		});
		if (!res.ok) {
			throw new Error(`Drive API list error (${res.status})`);
		}
		const payload = (await res.json()) as { nextPageToken?: string; files?: Array<Record<string, unknown>> };
		const files = payload.files ?? [];

		if (files.length > 0) {
			const stmt = env.DB.prepare(
				`INSERT OR REPLACE INTO files(account,id,name,mime,size,md5,created,modified,trashed,starred,shared,parent,owner,link,seen_run,deleted)
				 VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,0)`,
			);
			const batch = files.map((f) => {
				const parents = Array.isArray(f.parents) ? f.parents : [];
				const owners = Array.isArray(f.owners) ? (f.owners as Array<{ emailAddress?: string }>) : [];
				return stmt.bind(
					account,
					String(f.id ?? ""),
					String(f.name ?? ""),
					String(f.mimeType ?? ""),
					f.size ? Number(f.size) : null,
					f.md5Checksum ? String(f.md5Checksum) : null,
					f.createdTime ? String(f.createdTime) : null,
					f.modifiedTime ? String(f.modifiedTime) : null,
					f.trashed ? 1 : 0,
					f.starred ? 1 : 0,
					f.shared ? 1 : 0,
					parents[0] ? String(parents[0]) : null,
					owners[0]?.emailAddress ? String(owners[0].emailAddress) : null,
					f.webViewLink ? String(f.webViewLink) : null,
					runId,
				);
			});
			// D1 batch in chunks of 80
			for (let i = 0; i < batch.length; i += 80) {
				await env.DB.batch(batch.slice(i, i + 80));
			}
			total += files.length;
		}

		if (!payload.nextPageToken) break;
		pageToken = payload.nextPageToken;
	}

	const finished = new Date().toISOString().slice(0, 19).replace("T", " ");
	await env.DB.batch([
		env.DB.prepare("UPDATE files SET deleted=1 WHERE account=? AND seen_run<>?").bind(account, runId),
		env.DB.prepare("UPDATE runs SET finished=?, files=? WHERE run=?").bind(finished, total, runId),
	]);
	return total;
}

async function syncAllAccounts() {
	const pairs: Array<[string, string | undefined]> = [
		["cuenta1", env.REFRESH_TOKEN_CUENTA1],
		["cuenta2", env.REFRESH_TOKEN_CUENTA2],
	];
	for (const [acc, rt] of pairs) {
		if (rt && rt.trim()) {
			await syncAccount(acc, rt.trim());
		}
	}
}

async function computeFolderMetrics() {
	const res = await env.DB.prepare(
		"SELECT account, id, name, mime, size, parent, modified, owner, link, shared FROM files WHERE deleted=0 AND trashed=0",
	).all<FileRow>();
	const allRows = res.results ?? [];
	const byId = new Map<string, FileRow>();
	const children = new Map<string, string[]>();

	for (const r of allRows) {
		const k = `${r.account}:${r.id}`;
		byId.set(k, r);
		if (r.parent) {
			const pk = `${r.account}:${r.parent}`;
			const arr = children.get(pk) ?? [];
			arr.push(k);
			children.set(pk, arr);
		}
	}

	const memo = new Map<string, [number, number]>();
	function calc(k: string, depth = 0): [number, number] {
		const hit = memo.get(k);
		if (hit) return hit;
		if (depth > 60) return [0, 0];
		let fCount = 0;
		let bSum = 0;
		for (const ck of children.get(k) ?? []) {
			const ch = byId.get(ck);
			if (!ch) continue;
			if (ch.mime === "application/vnd.google-apps.folder") {
				const [cf, cb] = calc(ck, depth + 1);
				fCount += cf;
				bSum += cb;
			} else {
				fCount += 1;
				bSum += Number(ch.size || 0);
			}
		}
		memo.set(k, [fCount, bSum]);
		return [fCount, bSum];
	}

	for (const [k, r] of byId.entries()) {
		if (r.mime === "application/vnd.google-apps.folder") calc(k);
	}
	return { allRows, byId, memo };
}

const ICON_B64 =
	"iVBORw0KGgoAAAANSUhEUgAAALQAAAC0CAYAAAA9zQYyAAAFV0lEQVR42u3S128cVRSA8fmzaIKEFGKbAKKIIoooCkUUUUQRRRRRBAlpTnHiNHrvvUNCL4ZXeKMEp6ztXc/Odg93r2SEsOOdVXTP3Dn3e/gdnbfR3PNF9WY7BbSIao1WCmhB0CBogKABggYIGgQNEDTgX9CJGYAWUVI3C6AEQYOgAYIGCBroO+hmCmhB0NAVdNUMQAuCBkED/gZdMwugBEGDoAGCBgga6DPo2AxAC4IGQQMEDRA00G/QSSMFtCBoEDTgbdDTZgBaEDQIGiBogKABJUGv3FPzBqEQdN9O3Z0UBuH4HHTVLDkqUshzws757TBXVDEjL0O7ksLL8/0wV5RfzFU1CImg06GdVTUIKfCgB00E2hCTN0HXU2mDO2J18nhHzJVL0AMmAG2IKeSgR2N1iCnQoAdGp1FQhQi6bIakFdunj1qznUJIr1tI99NLVI7NIoiIdcYt3dGRiAd9yrZK34jJHwvdiaCJmagLH/RIJTPi8TjqBe4WVNDLR8qZEY6/et0unKC3ljMjHI+DznC/IILuWmZ+thei8V+vG+YS9JQZ0pZtmeqJYAoQdIY7SrdF0CDoo7V081RPBOO/LHeUD3q6lkoj6ICCFm4rl6CXbJrsiWD8l+WO6oPO8ggErSdo6ajlgx6ezIRgChB0xluqDvrk4YlMZh9t7zh8M3ubrLfUHfTGiSMiluJa6K6iQU+aIWmx+cH/Iwg95ruvZF/yQW8o/WvdLzNQ6r93DiLotT/PQLkggl60vpSuGZtBILr3lg26YhZBq3/qIDCSfYkH/diPHQRGbdCP/tBBoFQG/cj3HQRKLOgJMyQ8/F0HgZPozASdpBIe+raDwEl0Jhb0g9+0EThVQT/wdRuBUxX0/V+1ETg1Qd/3ZRuw3AddNotj9+5rA5br1kSCvmdvG7CcB10yw7W7v2gBluvWRIK+6/MWYKkI+s7PWoClIug7Pm0Bloqgb/+kBVgqgr7t4yZgqQj61o+agKUi6Fs+bAKW86APT1VT127+oAlYrlsTCfqm95uApSLoG99rApaKoG94twFYKoK+/p0GYKkI+rq3G4ClIuhr32oAloqgr3mzAVjOgz5khmtXv9EALNetiQR91et1wHIf9KRZHLvytTpguW5NJOhV5keAVVqCvuLVOmCpCPryV+qApSLoy16uAZZA0HHq2qUv1QDLdWvRQTNcu+TFGmC5bk0k6ItfqAGWiqAver4GWCqCvvC5GmC5D3rCLI5d8GwCWK5bEwn6/GcSwFIR9HlPJ4ClIuhzn0oAy3nQB8xw7ZwnE8By3ZpI0Gc/kQCWiqDP2lMFLBVBn7m7Clgqgj5jVxWwVAR9+s4qYLkPujSdunbajipguW5NJOiVozFgqQh6aHsMWM6DHjfDtcFtMWC5bk0k6K6BkRiBk+hMLOgVW2METlXQy7fECJyqoJdtRuhUBb10E0KnKuiuJcMIlVRj0fhhswhZvBGhkmos+tsMSYs2IDSSfYkHfdJ6hEZ10F0nrqsgENJt5RJ01wlrK1Auj65M0N0P5+P4x6FVXk3lGnTX2G+l9Lg1FSjRvWeePeUe9GzUXceurqCgZm+Yd0teBT2fY8xjwS8L3Sv3oPeb4YOFHgnF4ENH0f5DZvHE2K8lFJQvDXkVNFETs7qgiZqY1QVN2ISsMmjCJmSVQRM58aoPGpg36L/MALQgaBA04G/QB8spoAVBg6ABggYIGugz6D/NALQgaBA0QNAAQQP9Bn3ALIASBA2CBrwN+g8zAC0IGtqCnkoBLQgaBA0QNEDQQL9Bj5sFUCL63QxAC4IGQQMEDRA0QNAgaICgAYIGXPoH5tOj3G3XH5sAAAAASUVORK5CYII=";

export default {
	async scheduled(_controller, _env, ctx) {
		ctx.waitUntil(syncAllAccounts());
	},

	async fetch(request: Request, _env, ctx): Promise<Response> {
		const url = new URL(request.url);

		if (
			url.pathname === "/apple-touch-icon.png" ||
			url.pathname === "/apple-touch-icon-precomposed.png" ||
			url.pathname === "/favicon.ico"
		) {
			const bin = Uint8Array.from(atob(ICON_B64), (c) => c.charCodeAt(0));
			return new Response(bin, {
				headers: {
					"Content-Type": "image/png",
					"Cache-Control": "public, max-age=86400",
				},
			});
		}

		if (url.pathname === "/manifest.json") {
			return Response.json({
				name: "Drive Archotype Mac",
				short_name: "Archotype",
				start_url: "/",
				display: "standalone",
				background_color: "#f5f6f8",
				theme_color: "#f5f6f8",
				icons: [{ src: "/apple-touch-icon.png", sizes: "180x180", type: "image/png" }],
			});
		}

		if (url.pathname === "/" || url.pathname === "/index.html") {
			return new Response(HTML_PAGE, {
				headers: { "Content-Type": "text/html; charset=utf-8" },
			});
		}

		if (url.pathname.startsWith("/api/")) {
			const providedPin = request.headers.get("X-Pin") ?? url.searchParams.get("pin") ?? "";
			const expectedPin = env.PANEL_PIN ?? "";
			if (!expectedPin || !(await timingSafeEqual(providedPin, expectedPin))) {
				return Response.json({ error: "PIN inválido o requerido" }, { status: 401 });
			}

			if (url.pathname === "/api/download" && request.method === "GET") {
				const acc = url.searchParams.get("account") ?? "";
				const id = url.searchParams.get("id") ?? "";
				const rawName = url.searchParams.get("name") ?? "archivo";
				const mime = url.searchParams.get("mime") ?? "application/octet-stream";

				const rt = acc === "cuenta1" ? env.REFRESH_TOKEN_CUENTA1 : env.REFRESH_TOKEN_CUENTA2;
				if (!rt || !id) {
					return new Response("Parámetros inválidos", { status: 400 });
				}
				const tok = await getAccessToken(rt);

				let driveUrl = `https://www.googleapis.com/drive/v3/files/${encodeURIComponent(id)}?alt=media`;
				let outName = rawName;
				let outMime = mime;

				// Google Workspace native files need export
				if (mime.startsWith("application/vnd.google-apps.")) {
					if (mime.includes("spreadsheet")) {
						outMime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet";
						if (!outName.endsWith(".xlsx")) outName += ".xlsx";
					} else {
						outMime = "application/pdf";
						if (!outName.endsWith(".pdf")) outName += ".pdf";
					}
					driveUrl = `https://www.googleapis.com/drive/v3/files/${encodeURIComponent(id)}/export?mimeType=${encodeURIComponent(outMime)}`;
				}

				const reqHeaders: Record<string, string> = { Authorization: `Bearer ${tok}` };
				const rangeHdr = request.headers.get("Range");
				if (rangeHdr) reqHeaders["Range"] = rangeHdr;

				const upstream = await fetch(driveUrl, { headers: reqHeaders });
				if (!upstream.ok && upstream.status !== 206) {
					return new Response(`Error descargando de Drive (${upstream.status})`, { status: upstream.status });
				}

				const safeAscii = outName.replace(/[^\x20-\x7E]/g, "_").replace(/["\\]/g, "_");
				const resHeaders = new Headers();
				resHeaders.set("Content-Type", upstream.headers.get("Content-Type") || outMime);
				resHeaders.set(
					"Content-Disposition",
					`attachment; filename="${safeAscii}"; filename*=UTF-8''${encodeURIComponent(outName)}`,
				);
				const cl = upstream.headers.get("Content-Length");
				if (cl) resHeaders.set("Content-Length", cl);
				const cr = upstream.headers.get("Content-Range");
				if (cr) resHeaders.set("Content-Range", cr);

				return new Response(upstream.body, {
					status: upstream.status,
					headers: resHeaders,
				});
			}

			await ensureSchema();

			if (url.pathname === "/api/overview" && request.method === "GET") {
				const accRes = await env.DB.prepare(
					`SELECT account,
					 SUM(CASE WHEN mime<>'application/vnd.google-apps.folder' AND trashed=0 THEN 1 ELSE 0 END) files,
					 SUM(CASE WHEN mime='application/vnd.google-apps.folder' AND trashed=0 THEN 1 ELSE 0 END) folders,
					 COALESCE(SUM(CASE WHEN trashed=0 THEN size ELSE 0 END),0) bytes,
					 SUM(trashed) trashed, SUM(shared) shared
					 FROM files WHERE deleted=0 GROUP BY account`,
				).all();

				const lastRes = await env.DB.prepare("SELECT account, MAX(finished) finished FROM runs GROUP BY account").all();
				const dupRes = await env.DB.prepare(
					"SELECT COUNT(*) c FROM (SELECT md5 FROM files WHERE deleted=0 AND trashed=0 AND md5 IS NOT NULL GROUP BY md5 HAVING COUNT(*)>1)",
				).first<{ c: number }>();
				const lrgRes = await env.DB.prepare(
					"SELECT COUNT(*) c FROM files WHERE deleted=0 AND trashed=0 AND mime<>'application/vnd.google-apps.folder' AND size>=1048576",
				).first<{ c: number }>();

				const { byId, memo } = await computeFolderMetrics();
				const topFolders: Array<Record<string, unknown>> = [];
				for (const [k, r] of byId.entries()) {
					if (r.mime === "application/vnd.google-apps.folder") {
						const pk = r.parent ? `${r.account}:${r.parent}` : "";
						if (!pk || !byId.has(pk)) {
							const [fc, bs] = memo.get(k) ?? [0, 0];
							topFolders.push({
								account: r.account,
								id: r.id,
								name: r.name,
								files: fc,
								bytes: bs,
							});
						}
					}
				}
				topFolders.sort((a, b) => Number(b.bytes) - Number(a.bytes) || Number(b.files) - Number(a.files));

				return Response.json({
					accounts: (accRes.results ?? []).map((a) => ({ ...a, can_write: true })),
					last: lastRes.results ?? [],
					top_folders: topFolders.slice(0, 30),
					dup_count: dupRes?.c ?? 0,
					large_count: lrgRes?.c ?? 0,
				});
			}

			if (url.pathname === "/api/browse" && request.method === "GET") {
				const acc = url.searchParams.get("account") ?? "";
				const folderId = url.searchParams.get("folder_id") ?? "";
				const q = (url.searchParams.get("q") ?? "").trim().toLowerCase();

				const { allRows, byId, memo } = await computeFolderMetrics();
				const foldersOut: Array<Record<string, unknown>> = [];
				let filesOut: FileRow[] = [];
				const breadcrumbs: Array<{ account: string; id: string; name: string }> = [];

				if (q) {
					for (const r of allRows) {
						if (acc && r.account !== acc) continue;
						if ((r.name ?? "").toLowerCase().includes(q)) {
							if (r.mime === "application/vnd.google-apps.folder") {
								const [fc, bs] = memo.get(`${r.account}:${r.id}`) ?? [0, 0];
								foldersOut.push({ ...r, files: fc, bytes: bs });
							} else {
								filesOut.push(r);
							}
						}
					}
				} else if (folderId && acc) {
					let currKey = `${acc}:${folderId}`;
					let seen = 0;
					while (byId.has(currKey) && seen < 40) {
						const row = byId.get(currKey)!;
						breadcrumbs.push({ account: acc, id: row.id, name: row.name });
						currKey = row.parent ? `${acc}:${row.parent}` : "";
						seen++;
					}
					breadcrumbs.reverse();

					for (const r of allRows) {
						if (r.account === acc && r.parent === folderId) {
							if (r.mime === "application/vnd.google-apps.folder") {
								const [fc, bs] = memo.get(`${r.account}:${r.id}`) ?? [0, 0];
								foldersOut.push({ ...r, files: fc, bytes: bs });
							} else {
								filesOut.push(r);
							}
						}
					}
				} else {
					for (const r of allRows) {
						if (acc && r.account !== acc) continue;
						const pk = r.parent ? `${r.account}:${r.parent}` : "";
						if (!pk || !byId.has(pk)) {
							if (r.mime === "application/vnd.google-apps.folder") {
								const [fc, bs] = memo.get(`${r.account}:${r.id}`) ?? [0, 0];
								foldersOut.push({ ...r, files: fc, bytes: bs });
							} else {
								filesOut.push(r);
							}
						}
					}
					if (filesOut.length === 0) {
						filesOut = allRows
							.filter((r) => r.mime !== "application/vnd.google-apps.folder" && (!acc || r.account === acc))
							.sort((a, b) => String(b.modified ?? "").localeCompare(String(a.modified ?? "")))
							.slice(0, 60);
					}
				}

				foldersOut.sort((a, b) => Number(b.bytes) - Number(a.bytes));
				filesOut.sort((a, b) => String(b.modified ?? "").localeCompare(String(a.modified ?? "")));
				return Response.json({
					folders: foldersOut.slice(0, 60),
					files: filesOut.slice(0, 250),
					breadcrumbs,
				});
			}

			if (url.pathname === "/api/large" && request.method === "GET") {
				const minMb = Number(url.searchParams.get("min_mb") ?? "1");
				const acc = url.searchParams.get("account") ?? "";
				const minBytes = minMb * 1024 * 1024;
				const res = acc
					? await env.DB.prepare(
							"SELECT account,id,name,mime,size,modified,owner,link FROM files WHERE deleted=0 AND trashed=0 AND mime<>'application/vnd.google-apps.folder' AND size>=? AND account=? ORDER BY size DESC LIMIT 200",
						)
							.bind(minBytes, acc)
							.all()
					: await env.DB.prepare(
							"SELECT account,id,name,mime,size,modified,owner,link FROM files WHERE deleted=0 AND trashed=0 AND mime<>'application/vnd.google-apps.folder' AND size>=? ORDER BY size DESC LIMIT 200",
						)
							.bind(minBytes)
							.all();
				return Response.json(res.results ?? []);
			}

			if (url.pathname === "/api/duplicates" && request.method === "GET") {
				const grps = await env.DB.prepare(
					"SELECT md5, COUNT(*) n, MAX(size) size FROM files WHERE deleted=0 AND trashed=0 AND md5 IS NOT NULL GROUP BY md5 HAVING n>1 ORDER BY size*(n-1) DESC LIMIT 80",
				).all<{ md5: string; n: number; size: number }>();
				const out = [];
				for (const g of grps.results ?? []) {
					const fls = await env.DB.prepare(
						"SELECT account,id,name,mime,modified,owner,link FROM files WHERE md5=? AND deleted=0 AND trashed=0 ORDER BY modified",
					)
						.bind(g.md5)
						.all();
					out.push({ ...g, files: fls.results ?? [] });
				}
				return Response.json(out);
			}

			if (url.pathname === "/api/trash" && request.method === "POST") {
				const body = (await request.json()) as { items?: Array<{ account: string; id: string }> };
				const items = body.items ?? [];
				let done = 0;
				const errors: string[] = [];
				const tokenCache = new Map<string, string>();

				for (const it of items) {
					try {
						let tok = tokenCache.get(it.account);
						if (!tok) {
							const rt = it.account === "cuenta1" ? env.REFRESH_TOKEN_CUENTA1 : env.REFRESH_TOKEN_CUENTA2;
							if (!rt) throw new Error(`Falta token de ${it.account}`);
							tok = await getAccessToken(rt);
							tokenCache.set(it.account, tok);
						}
						const r = await fetch(`https://www.googleapis.com/drive/v3/files/${encodeURIComponent(it.id)}`, {
							method: "PATCH",
							headers: {
								Authorization: `Bearer ${tok}`,
								"Content-Type": "application/json",
							},
							body: JSON.stringify({ trashed: true }),
						});
						if (!r.ok) throw new Error(`HTTP ${r.status}`);
						await env.DB.prepare("UPDATE files SET trashed=1 WHERE account=? AND id=?").bind(it.account, it.id).run();
						done++;
					} catch (e) {
						errors.push(`${it.id}: ${(e as Error).message}`);
					}
				}
				return Response.json({ done, errors });
			}

			if (url.pathname === "/api/sync" && request.method === "POST") {
				await syncAllAccounts();
				return Response.json({ ok: true });
			}
		}

		return new Response("Not found", { status: 404 });
	},
} satisfies ExportedHandler;

const HTML_PAGE = `<!doctype html><html lang="es"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,maximum-scale=1,viewport-fit=cover">
<meta name="apple-mobile-web-app-capable" content="yes">
<meta name="apple-mobile-web-app-status-bar-style" content="default">
<meta name="apple-mobile-web-app-title" content="Archotype">
<meta name="theme-color" content="#f5f6f8">
<link rel="apple-touch-icon" sizes="180x180" href="/apple-touch-icon.png">
<link rel="icon" type="image/png" href="/apple-touch-icon.png">
<link rel="manifest" href="/manifest.json">
<title>Drive Archotype Mac</title><style>
:root {
  --outer: #e5e6eb;
  --shell: #f5f6f8;
  --panel: #ffffff;
  --card: #f8f9fb;
  --ink: #14171f;
  --ink-soft: #454b57;
  --mut: #858c9b;
  --line: #eceef2;
  --pri: #2f6feb;
  --red: #e5484d;
  --chk: #181a20;
}
* { box-sizing: border-box; -webkit-tap-highlight-color: transparent; }
html, body {
  margin: 0;
  padding: 0;
  width: 100%;
  max-width: 100vw;
  overflow-x: hidden;
}
body {
  font: 14px/1.45 -apple-system, BlinkMacSystemFont, "SF Pro Text", "Inter", Segoe UI, sans-serif;
  background: var(--outer);
  color: var(--ink);
  height: 100dvh;
  padding: 12px;
  overflow: hidden;
  -webkit-font-smoothing: antialiased;
}
.shell {
  display: grid;
  grid-template-columns: 256px minmax(0, 1fr);
  height: 100%;
  width: 100%;
  background: var(--shell);
  border-radius: 22px;
  padding: 10px;
  gap: 8px;
  box-shadow: 0 24px 60px rgba(15, 23, 42, 0.08), 0 0 0 1px rgba(255,255,255,0.7) inset;
}
.backdrop {
  display: none;
  position: fixed;
  inset: 0;
  background: rgba(15, 23, 42, 0.38);
  backdrop-filter: blur(2px);
  -webkit-backdrop-filter: blur(2px);
  z-index: 45;
  opacity: 0;
  transition: opacity .2s ease;
}
.backdrop.open {
  display: block;
  opacity: 1;
}
aside {
  display: flex;
  flex-direction: column;
  padding: 8px 10px 16px;
  overflow-y: auto;
  user-select: none;
}
.brand {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
  padding: 8px 10px 16px;
}
.brand-title { font-weight: 700; font-size: 15.5px; letter-spacing: -0.02em; flex: 1; }
.close-drawer-btn {
  display: none;
  border: 1px solid var(--line);
  background: #fff;
  color: var(--ink);
  width: 32px;
  height: 32px;
  border-radius: 9px;
  align-items: center;
  justify-content: center;
  font-size: 16px;
  cursor: pointer;
  flex-shrink: 0;
}
.sec-label { font-size: 11.5px; font-weight: 500; color: var(--mut); padding: 12px 12px 6px; }
.nav-item {
  display: flex; align-items: center; gap: 10px; padding: 9px 12px; border-radius: 10px;
  color: var(--ink-soft); cursor: pointer; font-weight: 500; font-size: 13.5px; margin-bottom: 2px;
}
.nav-item:hover { background: rgba(0,0,0,0.035); color: var(--ink); }
.nav-item.active {
  background: var(--panel); color: var(--ink); font-weight: 600;
  box-shadow: 0 2px 7px rgba(15, 23, 42, 0.05), 0 0 0 1px rgba(0,0,0,0.03);
}
.nav-item .cnt { margin-left: auto; font-size: 12px; color: var(--mut); padding-left: 6px; }
.tree-children { margin-left: 14px; padding-left: 8px; border-left: 1px solid #e2e5ec; margin-top: 2px; }

main {
  background: var(--panel); border-radius: 18px;
  box-shadow: 0 2px 12px rgba(15, 23, 42, 0.03), 0 0 0 1px rgba(0,0,0,0.035);
  display: flex; flex-direction: column; overflow: hidden; position: relative;
  min-width: 0;
}
.topbar {
  display: flex; align-items: center; justify-content: space-between;
  padding: 12px 18px; border-bottom: 1px solid var(--line); gap: 8px;
}
.top-left {
  display: flex; align-items: center; gap: 8px; min-width: 0; flex: 1;
}
.crumbs {
  display: flex; align-items: center; gap: 6px; font-size: 13px; color: var(--mut);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; min-width: 0;
}
.crumbs span.crumb { cursor: pointer; overflow: hidden; text-overflow: ellipsis; }
.crumbs span.crumb.current { color: var(--ink-soft); font-weight: 600; }
.top-actions { display: flex; align-items: center; gap: 8px; flex-shrink: 0; }
.search-box {
  display: flex; align-items: center; gap: 6px; background: var(--card);
  border: 1px solid var(--line); border-radius: 10px; padding: 6px 10px; width: 170px;
}
.search-box input { border: 0; background: transparent; outline: none; font-size: 13px; width: 100%; min-width: 0; }
.btn-sync {
  border: 1px solid var(--line); background: #fff; color: var(--ink); font-weight: 500;
  font-size: 13px; padding: 7px 11px; border-radius: 10px; cursor: pointer; white-space: nowrap;
}
.content { padding: 18px 20px 96px; overflow-y: auto; overflow-x: hidden; flex: 1; min-width: 0; }
.title-bar { display: flex; align-items: center; justify-content: space-between; margin-bottom: 16px; gap: 8px; flex-wrap: wrap; }
.title-left { display: flex; align-items: center; gap: 10px; min-width: 0; }
.title-left h1 { font-size: 18px; font-weight: 650; margin: 0; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.subhead { font-size: 13.5px; font-weight: 600; margin: 6px 0 12px; }
.subhead span { color: var(--mut); font-weight: 500; }
.ico {
  width: 17px; height: 17px; stroke: currentColor; stroke-width: 1.8;
  fill: none; stroke-linecap: round; stroke-linejoin: round; flex-shrink: 0;
}
.filter-wrap {
  margin-bottom: 14px;
}
.filter-split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  border-top: 1px solid var(--line);
  border-bottom: 1px solid var(--line);
  background: #fff;
}
.flt-cell {
  position: relative;
  display: flex;
  align-items: center;
  justify-content: center;
  gap: 8px;
  padding: 12px 10px;
  font-size: 14px;
  font-weight: 500;
  color: var(--ink);
  cursor: pointer;
  user-select: none;
  transition: background .15s;
}
.flt-cell:hover { background: var(--card); }
.flt-cell + .flt-cell { border-left: 1px solid var(--line); }
.flt-native {
  position: absolute;
  inset: 0;
  width: 100%;
  height: 100%;
  opacity: 0;
  cursor: pointer;
  font-size: 16px;
}
.chips-bar {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding: 10px 2px 4px;
}
.chip {
  display: inline-flex;
  align-items: center;
  gap: 7px;
  background: #eff1f5;
  color: #1f2433;
  border: 0;
  border-radius: 999px;
  padding: 6px 13px;
  font-size: 12.5px;
  font-weight: 600;
  cursor: pointer;
  transition: background .15s;
}
.chip:hover { background: #e3e6ee; }
.chip svg { width: 12px; height: 12px; stroke: #687082; stroke-width: 2.2; }
.flt-count { margin-left: auto; font-size: 12px; color: var(--mut); font-weight: 500; }

.folder-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(150px, 1fr)); gap: 12px; margin-bottom: 22px; }
.folder-card {
  background: var(--card); border: 1px solid #eef0f5; border-radius: 16px;
  padding: 14px 10px; text-align: center; cursor: pointer; position: relative; min-width: 0;
}
.folder-card .card-badge {
  position: absolute; top: 8px; right: 8px; font-size: 9.5px; color: var(--mut);
  background: #fff; padding: 2px 6px; border-radius: 999px; border: 1px solid var(--line);
}
.folder-svg { width: 68px; height: 56px; margin: 6px auto 8px; display: block; filter: drop-shadow(0 8px 14px rgba(59, 130, 246, 0.22)); }
.folder-name { font-weight: 600; font-size: 13px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.folder-meta { font-size: 11px; color: var(--mut); margin-top: 2px; white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* RESPONSIVE FILE TABLE */
.table-wrap {
  width: 100%;
  table-layout: fixed;
  border-collapse: collapse;
}
.table-wrap th {
  text-align: left; font-size: 12px; color: var(--mut);
  padding: 9px 6px; border-bottom: 1px solid var(--line);
}
.table-wrap td {
  padding: 11px 6px; border-bottom: 1px solid #f2f4f7;
  font-size: 13px; vertical-align: middle;
}
.col-chk { width: 32px; }
.col-date { width: 115px; }
.col-acc { width: 115px; }
.col-size { width: 82px; text-align: right; white-space: nowrap; }

.file-cell {
  display: flex; align-items: center; gap: 6px;
  width: 100%; min-width: 0;
}
.file-cell a {
  color: var(--ink); font-weight: 500; text-decoration: none;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis;
  display: block; flex: 1; min-width: 0;
}
.file-sub-mob {
  display: none;
  font-size: 11px;
  color: var(--mut);
  margin-top: 2px;
}

.chk {
  appearance: none; -webkit-appearance: none; width: 17px; height: 17px; border: 1.5px solid #cfd4e0;
  border-radius: 5px; background: #fff; cursor: pointer; display: inline-grid; place-content: center; margin: 0;
}
.chk:checked { background: var(--chk); border-color: var(--chk); }
.chk:checked::after {
  content: ""; width: 9px; height: 5px; border-left: 2px solid #fff; border-bottom: 2px solid #fff;
  transform: rotate(-45deg) translate(1px, -1px);
}
.owner-cell { display: inline-flex; align-items: center; gap: 6px; font-size: 12px; color: var(--ink-soft); max-width: 100%; overflow: hidden; }
.avatar {
  width: 22px; height: 22px; border-radius: 50%; display: inline-flex; align-items: center;
  justify-content: center; color: #fff; font-size: 10.5px; font-weight: 600; flex-shrink: 0;
}
.av-1 { background: linear-gradient(135deg, #6366f1, #ec4899); }
.av-2 { background: linear-gradient(135deg, #3b82f6, #06b6d4); }
.stats-row { display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr)); gap: 10px; margin-bottom: 18px; }
.stat-card { background: var(--card); border: 1px solid #eef0f5; border-radius: 15px; padding: 13px 14px; min-width: 0; }
.stat-card .big { font-size: 20px; font-weight: 700; margin: 3px 0; }
.bar { height: 6px; background: #e6e9f2; border-radius: 99px; overflow: hidden; margin-top: 7px; }
.bar i { display: block; height: 100%; background: linear-gradient(90deg, #3b82f6, #60a5fa); }
.action-bar {
  position: fixed; bottom: 16px; left: 50%; transform: translateX(-50%);
  width: calc(100% - 28px); max-width: 420px;
  background: #181a20; color: #fff; padding: 10px 14px; border-radius: 14px;
  display: flex; align-items: center; justify-content: space-between; gap: 10px;
  box-shadow: 0 18px 38px rgba(15, 23, 42, 0.28); z-index: 40;
}
.action-bar.hide { display: none; }
.action-bar .act-btn { border: 0; background: var(--red); color: #fff; font-weight: 600; font-size: 12.5px; padding: 8px 12px; border-radius: 9px; cursor: pointer; white-space: nowrap; }
.toast { padding: 10px 14px; border-radius: 11px; margin-bottom: 14px; background: #eef4ff; color: #1e3a8a; font-size: 13px; }
.mob-btn { display: none; }

@media (max-width: 780px) {
  body { padding: 0; background: var(--panel); }
  .shell { grid-template-columns: 1fr; border-radius: 0; padding: 0; gap: 0; box-shadow: none; }
  aside {
    position: fixed; inset: 0 auto 0 0; width: 278px; max-width: 84vw; background: var(--shell);
    z-index: 50; transform: translateX(-105%); transition: transform .22s cubic-bezier(0.22, 1, 0.36, 1);
    box-shadow: 14px 0 36px rgba(0,0,0,0.18);
    padding: 14px 12px 24px;
  }
  aside.open { transform: translateX(0); }
  .close-drawer-btn { display: inline-flex; }
  main { border-radius: 0; box-shadow: none; height: 100dvh; }
  .topbar { padding: 10px 14px; }
  .content { padding: 14px 14px 96px; }
  .mob-btn {
    display: inline-flex; align-items: center; justify-content: center;
    border: 1px solid var(--line); background: #fff; width: 34px; height: 34px;
    border-radius: 9px; font-size: 16px; cursor: pointer; flex-shrink: 0;
  }
  .search-box { width: 125px; padding: 5px 8px; }
  .hide-mob { display: none !important; }
  .col-acc { width: 34px; text-align: center; }
  .col-size { width: 72px; font-size: 11.5px; }
  .file-sub-mob { display: block; }
  .folder-grid { grid-template-columns: repeat(2, minmax(0, 1fr)); gap: 10px; }
}
</style></head><body>
<svg width="0" height="0" style="position:absolute">
  <defs>
    <linearGradient id="fBack" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#56a7f8"/><stop offset="100%" stop-color="#2e7de1"/></linearGradient>
    <linearGradient id="fFront" x1="0" y1="0" x2="0" y2="1"><stop offset="0%" stop-color="#8fd0ff" stop-opacity="0.92"/><stop offset="100%" stop-color="#4898f0" stop-opacity="0.97"/></linearGradient>
    <symbol id="mac-folder" viewBox="0 0 96 78">
      <path d="M8 16 C8 11, 12 8, 17 8 L35 8 C39 8, 41 10, 44 13 L48 17 L79 17 C84 17, 88 21, 88 26 L88 64 C88 69, 84 72, 79 72 L17 72 C12 72, 8 69, 8 64 Z" fill="url(#fBack)"/>
      <rect x="15" y="19" width="66" height="22" rx="4" fill="#ffffff" fill-opacity="0.88"/>
      <path d="M6 30 C6 25, 10 22, 15 22 L81 22 C86 22, 90 25, 90 30 L88 65 C88 70, 84 73, 79 73 L17 73 C12 73, 8 70, 8 65 Z" fill="url(#fFront)" stroke="#a8dcff" stroke-width="0.8"/>
    </symbol>
  </defs>
</svg>
<div class="backdrop" id="backdrop"></div>
<div class="shell">
  <aside id="drawer">
    <div class="brand">
      <span class="brand-title">Drive Archotype Mac</span>
      <button class="close-drawer-btn" id="closeDrawerBtn" aria-label="Cerrar menú"><svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg></button>
    </div>
    <div class="sec-label">Vistas</div>
    <div id="nav-top"></div>
    <div class="sec-label">Espacios de Drive</div>
    <div id="nav-spaces"></div>
  </aside>
  <main>
    <div class="topbar">
      <div class="top-left">
        <button class="mob-btn" id="menuBtn" aria-label="Abrir menú"><svg class="ico" viewBox="0 0 24 24"><path d="M4 7h16M4 12h16M4 17h16"/></svg></button>
        <div class="crumbs" id="crumbs"></div>
      </div>
      <div class="top-actions">
        <div class="search-box"><svg class="ico" style="width:14px;height:14px;color:var(--mut)" viewBox="0 0 24 24"><circle cx="11" cy="11" r="7"/><path d="M20 20l-3.5-3.5"/></svg><input type="text" id="searchInput" placeholder="Buscar…"></div>
        <button class="btn-sync" id="syncBtn" style="display:inline-flex;align-items:center;gap:6px"><svg class="ico" style="width:14px;height:14px" viewBox="0 0 24 24"><path d="M21 12a9 9 0 0 0-15.36-6.36L3 8m0-5v5h5M3 12a9 9 0 0 0 15.36 6.36L21 16m0 5v-5h-5"/></svg>Sync</button>
      </div>
    </div>
    <div class="content">
      <div id="toast"></div>
      <div class="title-bar">
        <div class="title-left"><h1 id="pageTitle">Dashboard</h1></div>
        <div class="mut" id="lastSync" style="font-size:11.5px"></div>
      </div>
      <div id="viewBody"></div>
    </div>
    <div class="action-bar hide" id="actionBar">
      <span id="selCount" style="font-size:13px">0 seleccionados</span>
      <div style="display:flex;gap:8px">
        <button class="act-btn" id="dlBarBtn" style="background:var(--pri);display:inline-flex;align-items:center;gap:6px"><svg class="ico" style="width:15px;height:15px" viewBox="0 0 24 24"><path d="M12 4v11m0 0l-4-4m4 4l4-4M4 15v2a3 3 0 0 0 3 3h10a3 3 0 0 0 3-3v-2"/></svg>Descargar</button>
        <button class="act-btn" id="actBtn" style="display:inline-flex;align-items:center;gap:6px"><svg class="ico" style="width:15px;height:15px" viewBox="0 0 24 24"><path d="M4 7h16M9 7V4a1 1 0 0 1 1-1h4a1 1 0 0 1 1 1v3m-8 0l1 12a2 2 0 0 0 2 2h4a2 2 0 0 0 2-2l1-12M10 11v5M14 11v5"/></svg>Papelera</button>
      </div>
    </div>
  </main>
</div>
<script>
const $ = s => document.querySelector(s);
const fmt = b => { b=Number(b)||0; for(const u of['B','KB','MB','GB','TB']){if(b<1024)return b.toFixed(b<10&&u!='B'?1:0)+' '+u;b/=1024} return b.toFixed(1)+' PB'; };
const esc = s => String(s??'').replace(/[&<>"']/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
const fmtDate = iso => { if(!iso)return '—'; const d=new Date(iso); return isNaN(d)?String(iso).slice(0,10):d.toLocaleDateString('en-GB',{day:'2-digit',month:'short',year:'numeric'}); };

function openDrawer(open) {
  $('#drawer').classList.toggle('open', open);
  $('#backdrop').classList.toggle('open', open);
}

function getPin() {
  let p = localStorage.getItem('archotype_pin') || '';
  if (!p) {
    p = prompt('Ingresa tu PIN de acceso a Drive Archotype Mac:') || '';
    if (p) localStorage.setItem('archotype_pin', p);
  }
  return p;
}

async function api(u, body) {
  const res = await fetch(u, {
    method: body ? 'POST' : 'GET',
    headers: { 'Content-Type': 'application/json', 'X-Pin': getPin() },
    body: body ? JSON.stringify(body) : undefined
  });
  if (res.status === 401) {
    localStorage.removeItem('archotype_pin');
    alert('PIN incorrecto.');
    location.reload();
    throw new Error('Unauthorized');
  }
  return res.json();
}

const state = {
  mode: 'dashboard',
  account: '',
  folderId: '',
  folderName: '',
  q: '',
  fileType: 'all',
  sortBy: 'date_desc',
  ov: null
};

function matchFileType(f, t) {
  if (!t || t === 'all') return true;
  const m = String(f.mime || '').toLowerCase();
  const n = String(f.name || '').toLowerCase();
  if (t === 'image') return m.startsWith('image/') || /\.(png|jpe?g|gif|webp|svg|heic)$/.test(n);
  if (t === 'video') return m.startsWith('video/') || /\.(mp4|mov|mkv|avi|webm)$/.test(n);
  if (t === 'pdf') return m.includes('pdf') || n.endsWith('.pdf');
  if (t === 'design') return /\.(psd|ai|aep|indd|fig|sketch)$/.test(n) || m.includes('photoshop') || m.includes('illustrator');
  if (t === 'doc') return m.includes('word') || m.includes('pages') || m.includes('document') || m.includes('spreadsheet') || m.includes('presentation') || /\.(pages|docx?|xlsx?|pptx?|txt|csv)$/.test(n);
  if (t === 'zip') return m.includes('zip') || m.includes('rar') || m.includes('compressed') || /\.(zip|rar|7z|tar|gz)$/.test(n);
  return true;
}

function applyFilterSort(rawFiles) {
  const filtered = rawFiles.filter(f => matchFileType(f, state.fileType));
  const sb = state.sortBy;
  filtered.sort((a, b) => {
    if (sb === 'date_desc') return String(b.modified || '').localeCompare(String(a.modified || ''));
    if (sb === 'date_asc') return String(a.modified || '').localeCompare(String(b.modified || ''));
    if (sb === 'size_desc') return (Number(b.size) || 0) - (Number(a.size) || 0);
    if (sb === 'size_asc') return (Number(a.size) || 0) - (Number(b.size) || 0);
    if (sb === 'name_asc') return String(a.name || '').localeCompare(String(b.name || ''));
    return 0;
  });
  return filtered;
}
function toast(m) { $('#toast').innerHTML = m ? '<div class="toast">'+m+'</div>' : ''; }
function ownerBadge(acc, em) {
  const is2 = String(acc).includes('2');
  const lbl = em ? em.split('@')[0] : (acc==='cuenta1'?'Trabajo':'Personal');
  const init = (acc==='cuenta1'?'T':acc==='cuenta2'?'P':lbl[0]).toUpperCase();
  return '<span class="owner-cell" title="'+esc(lbl)+'"><span class="avatar '+(is2?'av-2':'av-1')+'">'+esc(init)+'</span><span class="hide-mob">'+esc(lbl)+'</span></span>';
}

function setCrumbs(parts) {
  $('#crumbs').innerHTML = parts.map((p, idx) => {
    const isLast = idx === parts.length - 1;
    return (idx > 0 ? '<span>•</span>' : '') + '<span class="crumb '+(isLast?'current':'')+'" data-cidx="'+idx+'">'+esc(p.label)+'</span>';
  }).join('');
  $('#crumbs').querySelectorAll('[data-cidx]').forEach(el => {
    const fn = parts[Number(el.dataset.cidx)].onClick;
    if (fn) el.onclick = fn;
  });
}

function renderSidebar() {
  const ov = state.ov; if (!ov) return;
  const tot = ov.accounts.reduce((a,x)=>a+(x.files||0),0);
  const items = [
    { id:'dashboard', label:'Dashboard', cnt:tot, icon:'<svg class="ico" viewBox="0 0 24 24"><path d="M3 10.5L12 3l9 7.5V20a1 1 0 0 1-1 1h-5v-6H9v6H4a1 1 0 0 1-1-1v-9.5z"/></svg>' },
    { id:'browse-all', label:'Documentos', cnt:ov.top_folders.length, icon:'<svg class="ico" viewBox="0 0 24 24"><path d="M21 8v11a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2V8m18 0l-2.5-4H5.5L3 8m18 0H3m7 4h4"/></svg>' },
    { id:'large', label:'Archivos grandes', cnt:ov.large_count, icon:'<svg class="ico" viewBox="0 0 24 24"><path d="M21.21 15.89A10 10 0 1 1 8 2.83"/><path d="M22 12A10 10 0 0 0 12 2v10z"/></svg>' },
    { id:'dups', label:'Duplicados', cnt:ov.dup_count, icon:'<svg class="ico" viewBox="0 0 24 24"><rect x="8" y="8" width="12" height="12" rx="2.5"/><path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2"/></svg>' }
  ];
  $('#nav-top').innerHTML = items.map(it=>'<div class="nav-item '+(state.mode===it.id?'active':'')+'" data-nav="'+it.id+'">'+it.icon+'<span>'+it.label+'</span><span class="cnt">'+it.cnt+'</span></div>').join('');
  $('#nav-top').querySelectorAll('[data-nav]').forEach(el=>el.onclick=()=>{
    openDrawer(false);
    const k=el.dataset.nav; state.q=''; $('#searchInput').value='';
    if(k==='browse-all'){state.mode='browse';state.account='';state.folderId='';state.folderName='Documentos';}
    else{state.mode=k;state.account='';state.folderId='';}
    loadView();
  });

  const icoWork = '<svg class="ico" viewBox="0 0 24 24"><rect x="2" y="7" width="20" height="13" rx="2.5"/><path d="M16 7V5a2 2 0 0 0-2-2h-4a2 2 0 0 0-2 2v2m-6 5h20"/></svg>';
  const icoUser = '<svg class="ico" viewBox="0 0 24 24"><circle cx="12" cy="8" r="4"/><path d="M4 20c1.5-4 4.5-6 8-6s6.5 2 8 6"/></svg>';
  const icoFolder = '<svg class="ico" style="width:15px;height:15px" viewBox="0 0 24 24"><path d="M3 7a2 2 0 0 1 2-2h4l2 2h8a2 2 0 0 1 2 2v8a2 2 0 0 1-2 2H5a2 2 0 0 1-2-2z"/></svg>';

  $('#nav-spaces').innerHTML = ov.accounts.map(a=>{
    const sub = ov.top_folders.filter(f=>f.account===a.account);
    const lbl = a.account==='cuenta1'?'Trabajo · cuenta1':a.account==='cuenta2'?'Personal · cuenta2':a.account;
    const accIco = a.account==='cuenta1' ? icoWork : icoUser;
    return '<div class="nav-item" data-acc="'+esc(a.account)+'">'+accIco+'<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(lbl)+'</span><span class="cnt">'+a.files+'</span></div>'+
      '<div class="tree-children">'+sub.map(f=>'<div class="nav-item" data-facc="'+esc(a.account)+'" data-fid="'+esc(f.id)+'" data-fname="'+esc(f.name)+'">'+icoFolder+'<span style="overflow:hidden;text-overflow:ellipsis;white-space:nowrap">'+esc(f.name)+'</span><span class="cnt">'+f.files+'</span></div>').join('')+'</div>';
  }).join('');
  $('#nav-spaces').querySelectorAll('[data-acc]').forEach(el=>el.onclick=()=>{
    openDrawer(false);
    state.mode='browse';state.account=el.dataset.acc;state.folderId='';state.folderName=el.dataset.acc;loadView();
  });
  $('#nav-spaces').querySelectorAll('[data-fid]').forEach(el=>el.onclick=()=>{
    openDrawer(false);
    state.mode='browse';state.account=el.dataset.facc;state.folderId=el.dataset.fid;state.folderName=el.dataset.fname;loadView();
  });
}

function renderFolders(folders) {
  if (!folders || !folders.length) return '';
  return '<div class="subhead">Folders <span>'+folders.length+'</span></div><div class="folder-grid">'+
    folders.map(f=>'<div class="folder-card" data-oacc="'+esc(f.account)+'" data-oid="'+esc(f.id)+'" data-oname="'+esc(f.name)+'"><span class="card-badge">'+esc(f.account)+'</span><svg class="folder-svg"><use href="#mac-folder"/></svg><div class="folder-name">'+esc(f.name)+'</div><div class="folder-meta">'+f.files+' Files • '+fmt(f.bytes)+'</div></div>').join('')+'</div>';
}

function renderFiles(rawFiles) {
  $('#actionBar').classList.add('hide');
  const files = applyFilterSort(rawFiles);

  const typeOpts = [
    ['all', 'Todos los tipos'],
    ['image', 'Imágenes / SVG'],
    ['video', 'Videos (.mp4/.mov)'],
    ['design', 'Diseño (.psd/.ai/.aep)'],
    ['pdf', 'PDF'],
    ['doc', 'Docs / Pages'],
    ['zip', 'Comprimidos (.zip)']
  ];
  const sortOpts = [
    ['date_desc', 'Más recientes'],
    ['date_asc', 'Más viejos'],
    ['size_desc', 'Más pesados'],
    ['size_asc', 'Más livianos'],
    ['name_asc', 'Nombre (A-Z)']
  ];

  const typeLabel = (typeOpts.find(x => x[0] === state.fileType) || typeOpts[0])[1];
  const sortLabel = (sortOpts.find(x => x[0] === state.sortBy) || sortOpts[0])[1];
  const xSvg = '<svg class="ico" viewBox="0 0 24 24"><path d="M18 6L6 18M6 6l12 12"/></svg>';

  const barHtml = '<div class="filter-wrap">' +
    '<div class="filter-split">' +
      '<label class="flt-cell">' +
        '<svg class="ico" viewBox="0 0 24 24"><line x1="4" y1="6" x2="20" y2="6"/><line x1="4" y1="12" x2="20" y2="12"/><line x1="4" y1="18" x2="20" y2="18"/><circle cx="9" cy="6" r="2.2" fill="#fff"/><circle cx="15" cy="12" r="2.2" fill="#fff"/><circle cx="10" cy="18" r="2.2" fill="#fff"/></svg>' +
        '<span>Filter</span>' +
        '<select class="flt-native" id="selType">' +
          typeOpts.map(([k, v]) => '<option value="'+k+'" '+(state.fileType===k?'selected':'')+'>'+v+'</option>').join('') +
        '</select>' +
      '</label>' +
      '<label class="flt-cell">' +
        '<svg class="ico" viewBox="0 0 24 24"><path d="M6 4v16m0 0l-3-3m3 3l3-3"/><line x1="12" y1="6" x2="20" y2="6"/><line x1="12" y1="12" x2="18" y2="12"/><line x1="12" y1="18" x2="15" y2="18"/></svg>' +
        '<span>Sort</span>' +
        '<select class="flt-native" id="selSort">' +
          sortOpts.map(([k, v]) => '<option value="'+k+'" '+(state.sortBy===k?'selected':'')+'>'+v+'</option>').join('') +
        '</select>' +
      '</label>' +
    '</div>' +
    '<div class="chips-bar">' +
      '<button type="button" class="chip" id="chipType" title="Quitar o cambiar filtro">' + xSvg + '<span>'+esc(typeLabel)+'</span></button>' +
      '<button type="button" class="chip" id="chipSort" title="Restaurar orden">' + xSvg + '<span>'+esc(sortLabel)+'</span></button>' +
      '<span class="flt-count">'+files.length+' de '+rawFiles.length+'</span>' +
    '</div>' +
  '</div>';

  setTimeout(()=>{
    const st = $('#selType'), ss = $('#selSort');
    if (st) st.onchange = () => { state.fileType = st.value; loadView(); };
    if (ss) ss.onchange = () => { state.sortBy = ss.value; loadView(); };
    const ct = $('#chipType'), cs = $('#chipSort');
    if (ct) ct.onclick = () => {
      if (state.fileType !== 'all') { state.fileType = 'all'; loadView(); }
      else if (st && st.showPicker) st.showPicker();
    };
    if (cs) cs.onclick = () => {
      if (state.sortBy !== 'date_desc') { state.sortBy = 'date_desc'; loadView(); }
      else if (ss && ss.showPicker) ss.showPicker();
    };

    const chks=[...document.querySelectorAll('.item-chk')];
    const upd=()=>{
      const sel=chks.filter(c=>c.checked);
      if(sel.length){
        const s=sel.reduce((a,c)=>a+(Number(files[c.dataset.i].size)||0),0);
        $('#selCount').textContent=sel.length+' sel · '+fmt(s);
        $('#actionBar').classList.remove('hide');
      } else $('#actionBar').classList.add('hide');
    };
    chks.forEach(c=>c.onchange=upd);
    $('#actBtn').onclick=async()=>{
      const chosen=chks.filter(c=>c.checked).map(c=>files[c.dataset.i]);
      if(!confirm('¿Mandar '+chosen.length+' archivo(s) a la papelera de Drive?'))return;
      toast('Procesando…');
      const r=await api('/api/trash',{items:chosen.map(x=>({account:x.account,id:x.id}))});
      toast('Enviados '+r.done+' a la papelera.');
      loadView(true);
    };
    $('#dlBarBtn').onclick=()=>{
      const chosen=chks.filter(c=>c.checked).map(c=>files[c.dataset.i]);
      chosen.forEach((f, idx)=>{
        setTimeout(()=>{
          const u='/api/download?'+new URLSearchParams({account:f.account,id:f.id,name:f.name||'archivo',mime:f.mime||'',pin:getPin()}).toString();
          const a=document.createElement('a'); a.href=u; a.download=f.name||'archivo'; document.body.appendChild(a); a.click(); a.remove();
        }, idx*450);
      });
    };
  },0);

  if (!files.length) return barHtml + '<div class="stat-card mut">No hay archivos con ese filtro en esta vista.</div>';

  return barHtml + '<table class="table-wrap"><thead><tr><th class="col-chk"></th><th>File name</th><th class="col-date hide-mob">Date</th><th class="col-acc">Acct</th><th class="col-size">Size</th></tr></thead><tbody>'+
    files.map((f,i)=>
      '<tr><td class="col-chk"><input type="checkbox" class="chk item-chk" data-i="'+i+'"></td><td><div class="file-cell"><a href="'+esc(f.link)+'" target="_blank">'+esc(f.name)+'</a></div><div class="file-sub-mob">'+fmtDate(f.modified)+'</div></td><td class="col-date hide-mob">'+fmtDate(f.modified)+'</td><td class="col-acc">'+ownerBadge(f.account,f.owner)+'</td><td class="col-size" style="font-weight:600">'+fmt(f.size)+'</td></tr>'
    ).join('')+'</tbody></table>';
}

async function loadView(force=false) {
  if (!state.ov || force) {
    state.ov = await api('/api/overview');
    const last = state.ov.last.map(l=>l.finished).filter(Boolean).sort().pop();
    $('#lastSync').textContent = last ? 'Sync: '+last : '';
  }
  renderSidebar();
  $('#viewBody').innerHTML = '<div class="mut">Cargando…</div>';

  if (state.mode === 'dashboard') {
    $('#pageTitle').textContent = 'Dashboard';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Overview' }]);
    const d = await api('/api/browse');
    const stats = '<div class="stats-row">'+state.ov.accounts.map(a=>'<div class="stat-card"><div class="mut" style="font-size:12px">'+(a.account==='cuenta1'?'Trabajo · cuenta1':'Personal · cuenta2')+'</div><div class="big">'+fmt(a.bytes)+'</div><div class="mut" style="font-size:12px">'+a.files+' archivos · '+a.folders+' carpetas</div><div class="bar"><i style="width:'+Math.min(100,(a.bytes/(15*1073741824))*100)+'%"></i></div></div>').join('')+'</div>';
    $('#viewBody').innerHTML = stats + renderFolders(state.ov.top_folders.slice(0,6)) + '<div class="subhead">Recientes</div>' + renderFiles(d.files);
  } else if (state.mode === 'browse') {
    $('#pageTitle').textContent = state.q ? 'Búsqueda: '+state.q : (state.folderName || 'Documentos');
    const p = new URLSearchParams({account:state.account,folder_id:state.folderId,q:state.q});
    const d = await api('/api/browse?'+p.toString());
    const crumbs = [{ label: 'Docs', onClick: () => { state.mode='browse'; state.account=''; state.folderId=''; state.folderName='Documentos'; loadView(); } }];
    d.breadcrumbs.forEach(b => crumbs.push({ label: b.name, onClick: () => { state.mode='browse'; state.account=b.account; state.folderId=b.id; state.folderName=b.name; loadView(); } }));
    setCrumbs(crumbs);
    $('#viewBody').innerHTML = renderFolders(d.folders) + renderFiles(d.files);
  } else if (state.mode === 'large') {
    $('#pageTitle').textContent = 'Archivos grandes (> 1 MB)';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Grandes' }]);
    const d = await api('/api/large?min_mb=1');
    $('#viewBody').innerHTML = renderFiles(d);
  } else if (state.mode === 'dups') {
    $('#pageTitle').textContent = 'Duplicados';
    setCrumbs([{ label: 'Docs', onClick: () => { state.mode='dashboard'; loadView(); } }, { label: 'Duplicados' }]);
    const g = await api('/api/duplicates');
    const flat = []; g.forEach(x=>x.files.forEach(f=>flat.push({...f,size:x.size})));
    $('#viewBody').innerHTML = renderFiles(flat);
  }

  document.querySelectorAll('[data-oid]').forEach(c=>c.onclick=()=>{
    state.mode='browse';state.account=c.dataset.oacc;state.folderId=c.dataset.oid;state.folderName=c.dataset.oname;loadView();
  });
}

$('#menuBtn').onclick = () => openDrawer(true);
$('#closeDrawerBtn').onclick = () => openDrawer(false);
$('#backdrop').onclick = () => openDrawer(false);

let tmr=null;
$('#searchInput').oninput = e => { clearTimeout(tmr); tmr=setTimeout(()=>{state.q=e.target.value.trim();state.mode='browse';loadView();},250); };
$('#syncBtn').onclick = async () => {
  $('#syncBtn').disabled=true; toast('Sincronizando con Google Drive…');
  await api('/api/sync',{}); toast('✅ Sincronizado'); $('#syncBtn').disabled=false; loadView(true);
};
loadView(true);
</script></body></html>`;
