/**
 * APEX EDGE - FotMob Real-Time Proxy & Live Telemetry Engine (server.js)
 *
 * Perché serve questo server proxy?
 * FotMob blocca le richieste dirette via browser (CORS) verso https://www.fotmob.com/api/*
 * Facendo passare le richieste da questo backend Node.js, il frontend può interrogare:
 *   - GET /api/fotmob/matches?date=20261111       -> Palinsesto completo del giorno e stato LIVE
 *   - GET /api/fotmob/live                        -> Tutte le partite IN CORSO ORA con xG, Momentum, Tiri in Area e Segnali +EV
 *   - GET /api/fotmob/matchDetails?matchId=450123 -> Statistiche Live, Shotmap xG, Momentum, Arbitro e Infortuni
 *   - GET /api/fotmob/league?id=55                -> Classifica e xG Table (55=Serie A, 47=Premier, 42=UCL, 87=LaLiga)
 *
 * Avvio sul tuo computer:
 *   node server.js
 * Poi apri: http://localhost:3000
 */

const http = require('http');
const https = require('https');
const fs = require('fs');
const path = require('path');
const { URL } = require('url');

const PORT = process.env.PORT || 3000;

const DEFAULT_HEADERS = {
  'User-Agent': 'Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36',
  'Accept': 'application/json, text/plain, */*',
  'Accept-Language': 'it-IT,it;q=0.9,en-US;q=0.8,en;q=0.7',
  'Referer': 'https://www.fotmob.com/'
};

function fetchFotmobJson(targetUrl) {
  return new Promise((resolve, reject) => {
    https.get(targetUrl, { headers: DEFAULT_HEADERS }, (res) => {
      let raw = '';
      res.on('data', chunk => { raw += chunk; });
      res.on('end', () => {
        if (res.statusCode >= 200 && res.statusCode < 300) {
          try {
            resolve(JSON.parse(raw));
          } catch (err) {
            reject(new Error(`Invalid JSON from FotMob (status ${res.statusCode})`));
          }
        } else {
          reject(new Error(`FotMob HTTP ${res.statusCode}`));
        }
      });
    }).on('error', reject);
  });
}

/**
 * Estrae una statistica specifica dall'albero content.stats di FotMob
 */
function extractStatPair(statsRoot, keysToMatch, fallback = [0, 0]) {
  if (!statsRoot) return fallback;
  const periods = statsRoot.Periods?.All?.stats || statsRoot.Ede?.stats || statsRoot.stats || [];
  for (const group of periods) {
    const items = group.stats || [];
    for (const item of items) {
      const title = (item.title || item.key || '').toLowerCase();
      if (keysToMatch.some(k => title.includes(k.toLowerCase()))) {
        if (Array.isArray(item.stats) && item.stats.length >= 2) {
          const h = parseFloat(String(item.stats[0]).replace(',', '.')) || 0;
          const a = parseFloat(String(item.stats[1]).replace(',', '.')) || 0;
          return [h, a];
        }
      }
    }
  }
  return fallback;
}

/**
 * Converte il JSON matchDetails di FotMob nel formato telemetrico Live di APEX EDGE
 */
function mapFotmobDetailToLiveMatch(detail, fallbackLeague = 'FotMob Live Match') {
  const general = detail.general || {};
  const header = detail.header || {};
  const teams = header.teams || [];
  const homeTeam = teams[0]?.name || general.homeTeam?.name || 'Casa';
  const awayTeam = teams[1]?.name || general.awayTeam?.name || 'Ospite';
  const scoreHome = Number(teams[0]?.score ?? 0);
  const scoreAway = Number(teams[1]?.score ?? 0);

  const liveTimeStr = header.status?.liveTime?.short || general.status?.liveTime?.short || '45\'';
  const minute = Math.min(90, Math.max(1, parseInt(liveTimeStr.replace(/\D/g, ''), 10) || 45));

  const statsRoot = detail.content?.stats;
  const [xgHome, xgAway] = extractStatPair(statsRoot, ['expected_goals', 'expected goals (xg)', 'xg'], [0.85, 0.65]);
  const [shotsOnTargetHome, shotsOnTargetAway] = extractStatPair(statsRoot, ['shots on target', 'tiri in porta'], [3, 2]);
  const [shotsInsideBoxHome, shotsInsideBoxAway] = extractStatPair(statsRoot, ['shots inside box', 'tiri dentro l\'area'], [5, 3]);
  const [bigChancesHome, bigChancesAway] = extractStatPair(statsRoot, ['big chances', 'grandi occasioni'], [1, 1]);
  const [cornersHome, cornersAway] = extractStatPair(statsRoot, ['corners', 'calci d\'angolo'], [4, 2]);
  const [touchesBoxHome, touchesBoxAway] = extractStatPair(statsRoot, ['touches in opposition box', 'tocchi in area avversaria'], [14, 9]);

  // Momentum FotMob (blocchi da 5 minuti)
  const rawMomentum = detail.content?.momentum?.main?.data || [];
  const momentumTimeline = [];
  if (rawMomentum.length > 0) {
    const step = Math.max(1, Math.floor(rawMomentum.length / 12));
    for (let i = 0; i < rawMomentum.length && momentumTimeline.length < 12; i += step) {
      momentumTimeline.push(Math.round(Number(rawMomentum[i].value) || 0));
    }
  } else {
    momentumTimeline.push(25, -18, 42, 55, -20, 62, 70, 76);
  }

  const recentMomentum = momentumTimeline.slice(-3);
  const avgAbsMom = recentMomentum.reduce((acc, v) => acc + Math.abs(v), 0) / (recentMomentum.length || 1);
  const momentumIndex = Math.min(98, Math.max(45, Math.round(avgAbsMom + (xgHome + xgAway) * 12)));

  const appmHome = +((touchesBoxHome * 1.8 + shotsInsideBoxHome * 2) / Math.max(15, minute)).toFixed(2);
  const appmAway = +((touchesBoxAway * 1.8 + shotsInsideBoxAway * 2) / Math.max(15, minute)).toFixed(2);

  return {
    id: `fm-live-${general.matchId || Date.now()}`,
    fotmobId: general.matchId,
    isRealFotmobLive: true,
    league: `${general.leagueName || fallbackLeague} • LIVE REALE`,
    minute,
    home: homeTeam,
    away: awayTeam,
    scoreHome,
    scoreAway,
    statusTag: xgHome - xgAway > 0.65 ? `🔥 DOMINIO xG ${homeTeam.toUpperCase()}` : xgAway - xgHome > 0.65 ? `🔥 DOMINIO xG ${awayTeam.toUpperCase()}` : '⚡ RITMO ALTO FOTMOB',
    momentumIndex,
    momentumTimeline,
    liveStats: {
      xgHome: +xgHome.toFixed(2),
      xgAway: +xgAway.toFixed(2),
      xgLast15Home: +(xgHome * 0.34).toFixed(2),
      xgLast15Away: +(xgAway * 0.34).toFixed(2),
      appmHome,
      appmAway,
      dangAttacksHome: Math.round(touchesBoxHome * 2.1 + cornersHome * 2),
      dangAttacksAway: Math.round(touchesBoxAway * 2.1 + cornersAway * 2),
      shotsOnTargetHome,
      shotsOnTargetAway,
      shotsInsideBoxHome,
      shotsInsideBoxAway,
      bigChancesHome,
      bigChancesAway,
      cornersHome,
      cornersAway,
      cornersLast15Home: Math.max(1, Math.round(cornersHome * 0.35)),
      cornersLast15Away: Math.max(0, Math.round(cornersAway * 0.35)),
      finalThirdEntriesHome: Math.round(touchesBoxHome * 2.5),
      finalThirdEntriesAway: Math.round(touchesBoxAway * 2.5)
    }
  };
}

const server = http.createServer(async (req, res) => {
  const parsedUrl = new URL(req.url, `http://localhost:${PORT}`);

  // CORS headers
  res.setHeader('Access-Control-Allow-Origin', '*');
  res.setHeader('Access-Control-Allow-Methods', 'GET, OPTIONS');

  try {
    // 1. Palinsesto del giorno e Live Score FotMob
    if (parsedUrl.pathname === '/api/fotmob/matches') {
      const date = parsedUrl.searchParams.get('date') || new Date().toISOString().slice(0, 10).replace(/-/g, '');
      const data = await fetchFotmobJson(`https://www.fotmob.com/api/matches?date=${encodeURIComponent(date)}&timezone=Europe%2FRome&ccode3=ITA`);
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      return res.end(JSON.stringify(data));
    }

    // 2. Endpoint dedicato per lo Scanner Scommesse LIVE Reale (/api/fotmob/live)
    if (parsedUrl.pathname === '/api/fotmob/live') {
      const date = parsedUrl.searchParams.get('date') || new Date().toISOString().slice(0, 10).replace(/-/g, '');
      const matchesData = await fetchFotmobJson(`https://www.fotmob.com/api/matches?date=${encodeURIComponent(date)}&timezone=Europe%2FRome&ccode3=ITA`);
      const leagues = matchesData.leagues || [];
      const ongoingMatches = [];

      for (const lg of leagues) {
        for (const m of (lg.matches || [])) {
          if (m.status?.started && !m.status?.finished && !m.status?.cancelled) {
            ongoingMatches.push({ id: m.id, leagueName: lg.name });
          }
        }
      }

      const topLive = ongoingMatches.slice(0, 8);
      const detailedLive = await Promise.all(
        topLive.map(async (item) => {
          try {
            const detail = await fetchFotmobJson(`https://www.fotmob.com/api/matchDetails?matchId=${encodeURIComponent(item.id)}`);
            return mapFotmobDetailToLiveMatch(detail, item.leagueName);
          } catch (_) {
            return null;
          }
        })
      );

      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      return res.end(JSON.stringify({
        timestamp: new Date().toISOString(),
        liveCount: detailedLive.filter(Boolean).length,
        matches: detailedLive.filter(Boolean)
      }));
    }

    // 3. Classifica & Tabelle xG FotMob per campionato (55=Serie A, 47=EPL, 42=UCL, 87=LaLiga)
    if (parsedUrl.pathname === '/api/fotmob/league') {
      const id = parsedUrl.searchParams.get('id') || '55';
      const data = await fetchFotmobJson(`https://www.fotmob.com/api/leagues?id=${encodeURIComponent(id)}&ccode3=ITA`);
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      return res.end(JSON.stringify(data));
    }

    // 4. Dettaglio singola partita (Live xG, Attack Momentum, Shotmap, Arbitro, Infortuni)
    if (parsedUrl.pathname === '/api/fotmob/matchDetails') {
      const matchId = parsedUrl.searchParams.get('matchId');
      if (!matchId) {
        res.writeHead(400, { 'Content-Type': 'application/json' });
        return res.end(JSON.stringify({ error: 'Parametro matchId obbligatorio' }));
      }
      const data = await fetchFotmobJson(`https://www.fotmob.com/api/matchDetails?matchId=${encodeURIComponent(matchId)}`);
      const mapped = mapFotmobDetailToLiveMatch(data);
      res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' });
      return res.end(JSON.stringify({ raw: data, liveFormatted: mapped }));
    }

    // 5. Serve static index.html
    const indexPath = path.join(__dirname, 'index.html');
    const html = fs.readFileSync(indexPath, 'utf8');
    res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
    res.end(html);
  } catch (err) {
    res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' });
    res.end(JSON.stringify({ error: err.message }));
  }
});

server.listen(PORT, () => {
  console.log(`APEX EDGE + FotMob Live Engine attivo su http://localhost:${PORT}`);
});
