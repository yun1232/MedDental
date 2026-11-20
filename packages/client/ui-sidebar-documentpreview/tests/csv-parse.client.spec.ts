/** Delimited medical data stays literal and bounded independently from Office viewers. */
import { describe, expect, it } from 'vitest'
import { parseCsv } from '../src/client/csv/parse.ts'
const limits = { maxBytes: 1024, maxCells: 100, maxRows: 20 }
const bytes = (text: string) => new TextEncoder().encode(text)
describe('CSV/TSV preview', () => {
  it('preserves quoted commas, newlines, leading zeroes and formula-looking fields', () => {
    expect(parseCsv(bytes('id,note,value\n001,"a,b\nc",=1+1'), 'data.csv', limits).rows)
      .toEqual([['id', 'note', 'value'], ['001', 'a,b\nc', '=1+1']])
  })
  it('reads tab-separated Chinese fields', () => {
    expect(parseCsv(bytes('部位\t面积\n舌缘\t2.5'), 'data.tsv', limits).rows[1]).toEqual(['舌缘', '2.5'])
  })
  it('stops at row and total cell bounds without allocating empty matrices', () => {
    expect(parseCsv(bytes('a,b\nc,d\ne,f'), 'x.csv', { ...limits, maxRows: 1 })).toEqual({ rows: [['a', 'b']], truncated: true })
    expect(parseCsv(bytes('a,b\nc,d'), 'x.csv', { ...limits, maxCells: 3 })).toEqual({ rows: [['a', 'b']], truncated: true })
  })
  it('rejects oversized and malformed input', () => {
    expect(() => parseCsv(bytes('too long'), 'x.csv', { ...limits, maxBytes: 2 })).toThrow('byte limit')
    expect(() => parseCsv(bytes('"unfinished'), 'x.csv', limits)).toThrow('Invalid')
    expect(() => parseCsv(new Uint8Array([255]), 'x.csv', limits)).toThrow()
  })
  it('decodes UTF-16 with a BOM', () => {
    expect(parseCsv(new Uint8Array([255, 254, 97, 0, 44, 0, 98, 0]), 'x.csv', limits).rows).toEqual([['a', 'b']])
  })
})
