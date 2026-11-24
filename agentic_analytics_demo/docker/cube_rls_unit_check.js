// Offline unit checks of cube.js RLS logic (no Cube needed): node docker/cube_rls_unit_check.js
const path = process.argv[2] || require('path').join(__dirname, 'cube_schema', 'cube.js');
process.env.CUBE_SQL_USERS = JSON.stringify({ admin: { password: 'a', securityContext: { role: 'admin' } }, rep01: { password: 'r', securityContext: { rep_id: 'REP01' } } });
const cfg = require(path);
const assert = require('assert');
const q = () => ({ measures: ['Sales.total_revenue'], dimensions: ['Courses.course_name'], filters: [] });
assert.deepStrictEqual(cfg.queryRewrite(q(), { securityContext: { role: 'admin' } }).filters, []);
const r = cfg.queryRewrite(q(), { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(r.filters, [{ member: 'Sales.rep_id', operator: 'equals', values: ['REP01'] }]);
const t = cfg.queryRewrite({ measures: ['SalesTargets.annual_target'], dimensions: ['SalesReps.rep_name'] }, { securityContext: { rep_id: 'REP02' } });
assert.strictEqual(t.filters.length, 2);
const c = cfg.queryRewrite({ dimensions: ['Courses.course_name'] }, { securityContext: { rep_id: 'REP02' } });
assert.deepStrictEqual(c.filters, []);
// SQL API member expressions (shapes captured from Cube v1.8.32)
const expr = (cube, name, sql, params) => ({ cubeName: cube, name, expressionName: name, expression: [...params, `return \`${sql}\``],
  definition: JSON.stringify({ cubeName: cube, alias: name, expr: { type: 'SqlFunction', cubeParams: params, sql } }) });
const e1 = cfg.queryRewrite({ measures: [expr('Sales', 'count_uint8_1__', 'COUNT(*)', ['Sales'])], dimensions: [], segments: [], filters: [], timeDimensions: [], subqueryJoins: [] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e1.filters, [{ member: 'Sales.rep_id', operator: 'equals', values: ['REP01'] }]);
const e2 = cfg.queryRewrite({ measures: [], dimensions: [expr('Courses', 'x', '${Courses.course_name} || ${SalesTargets.rep_id}', ['Courses', 'SalesTargets'])], filters: [] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e2.filters.map((f) => f.member), ['SalesTargets.rep_id']);
const e3 = cfg.queryRewrite({ dimensions: ['Courses.course_name'], segments: [expr('Courses', 'seg', '(UPPER(${Courses.course_name}) = $0$)', ['Courses'])], filters: [] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e3.filters, []);
const e4 = cfg.queryRewrite({ dimensions: ['Courses.course_name'], filters: [], subqueryJoins: [{ sql: 'SELECT "SalesReps".rep_id FROM ...', on: '', joinType: 'LEFT', alias: 'sq' }] }, { securityContext: { rep_id: 'REP01' } });
assert.deepStrictEqual(e4.filters.map((f) => f.member), ['SalesReps.rep_id']);
assert.throws(() => cfg.queryRewrite(q(), { securityContext: {} }), /Access denied/);
(async () => {
  assert.deepStrictEqual(await cfg.checkSqlAuth({}, 'rep01'), { password: 'r', securityContext: { rep_id: 'REP01' } });
  await assert.rejects(cfg.checkSqlAuth({}, 'nobody'), /Incorrect/);
  console.log('cube.js RLS unit checks: OK');
})();
