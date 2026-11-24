// Row-level security (RLS) for the Cube semantic layer.
//
// Security context:
//   REST / GraphQL: claims of the JWT signed with CUBEJS_API_SECRET, e.g. {"rep_id": "REP01"}
//   SQL API (port 15432): looked up per SQL user in CUBE_SQL_USERS (see docker/.env.example)
// role "admin" sees everything; every other context must carry rep_id and is filtered to it.
// RLS only applies to queries that go THROUGH Cube. Direct Postgres or DuckDB access bypasses it.

const RLS_MEMBERS = {
  Sales: 'Sales.rep_id',
  SalesTargets: 'SalesTargets.rep_id',
  SalesReps: 'SalesReps.rep_id',
};

const RLS_CUBES = Object.keys(RLS_MEMBERS);

// Which cubes a query touches. A member is either a "Cube.member" string (REST/GraphQL and simple
// SQL API queries) or, when the SQL API turns SQL into member expressions (functions, count(*),
// subqueries, WHERE on an expression), an object with cubeName plus the cubes its SQL references.
// Objects and subqueryJoins are also scanned for RLS cube names, so an unfamiliar shape adds the
// filter (fail closed) instead of skipping it.
function cubesInQuery(query) {
  const cubes = new Set();
  const addRlsCubesNamedIn = (value) => {
    const text = JSON.stringify(value);
    for (const cube of RLS_CUBES) {
      if (new RegExp(`(^|[^A-Za-z0-9_])${cube}([^A-Za-z0-9_]|$)`).test(text)) cubes.add(cube);
    }
  };
  const addMember = (m) => {
    if (typeof m === 'string') {
      cubes.add(m.split('.')[0]);
    } else if (m && typeof m === 'object') {
      if (typeof m.cubeName === 'string') cubes.add(m.cubeName);
      addRlsCubesNamedIn(m);
    }
  };
  const addFilters = (filters) => {
    for (const f of filters || []) {
      addMember(f.member);
      addMember(f.dimension);
      addFilters(f.and);
      addFilters(f.or);
    }
  };
  for (const m of [...(query.measures || []), ...(query.dimensions || []), ...(query.segments || [])]) {
    addMember(m);
  }
  for (const td of query.timeDimensions || []) addMember(td.dimension);
  addFilters(query.filters);
  if (query.subqueryJoins && query.subqueryJoins.length) addRlsCubesNamedIn(query.subqueryJoins);
  return cubes;
}

module.exports = {
  queryRewrite: (query, { securityContext }) => {
    const ctx = securityContext || {};
    if (ctx.role === 'admin') {
      return query;
    }
    if (!ctx.rep_id) {
      throw new Error('Access denied: security context needs role=admin or rep_id');
    }
    const used = cubesInQuery(query);
    query.filters = query.filters || [];
    for (const [cube, member] of Object.entries(RLS_MEMBERS)) {
      if (used.has(cube)) {
        query.filters.push({ member, operator: 'equals', values: [String(ctx.rep_id)] });
      }
    }
    return query;
  },

  checkSqlAuth: async (req, username) => {
    let users;
    try {
      users = JSON.parse(process.env.CUBE_SQL_USERS || '{}');
    } catch (e) {
      throw new Error('CUBE_SQL_USERS is not valid JSON');
    }
    const user = users[username];
    if (!user || !user.password) {
      throw new Error('Incorrect user name or password');
    }
    return { password: user.password, securityContext: user.securityContext || {} };
  },
};
