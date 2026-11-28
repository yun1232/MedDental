import { describe, expect, it } from 'vitest'
import { runBatchEpisode, doctorMessage } from '../batch-driver.mjs'

const data = { id: '44', mode: 'patient', max_turns: 3, facts: { '既往史': '正畸结束后', '病因': '龋性白斑 ICDAS 2' }, metadata: {} }

describe('batch information boundaries and dialogue', () => {
  it('passes only patient utterances to the existing consultation, preserves questions and stops on approval', async () => {
    const calls: any[] = [], simulations: any[] = []
    const result = await runBatchEpisode(data, {
      images: [{ ref: 'image44' }],
      simulate: async (kind: string, value: any) => {
        simulations.push({ kind, value })
        return kind === 'portrait' ? { opening: '牙上有白色斑，想咨询一下' } : { answer: '我刚结束正畸', unknown: false }
      },
      consult: async (input: any) => {
        calls.push(input)
        return calls.length === 1 ? { status: 'needs_input', next_question: '做过正畸吗？' }
          : { status: 'awaiting_doctor', run_id: 'run', revision: 2 }
      },
      propose: async () => ({ treatment_plan: '待评估方案', abstain: false, reason: '待医生核查' }),
    })
    expect(simulations[0].value.facts).toEqual(data.facts)
    expect(JSON.stringify(calls)).not.toContain('ICDAS')
    expect(calls[1].messages.map((m: any) => m.text)).toEqual(['牙上有白色斑，想咨询一下', '交流 Agent 的问题：做过正畸吗？', '我刚结束正畸'])
    expect(calls.every(c => c.images[0].ref === 'image44')).toBe(true)
    expect(result.turns).toBe(2)
    expect(result.proposal.abstain).toBe(false)
  })

  it('doctor mode exposes all clinical columns once without constructing a simulated patient', async () => {
    const doctor = { ...data, mode: 'doctor' }
    const result = await runBatchEpisode(doctor, {
      images: [], simulate: async () => { throw Error('Unexpected simulator') },
      consult: async ({ messages }: any) => {
        expect(messages[0].text).toBe(doctorMessage(doctor))
        expect(messages[0].text).toContain('ICDAS')
        return { status: 'needs_input', next_question: '没有记录的信息？' }
      }, propose: async () => { throw Error('Must abstain before approval') },
    })
    expect(result.turns).toBe(1)
    expect(result.proposal.abstain).toBe(true)
  })

  it('unknown replies stay unknown and a turn limit yields an abstention, not a made-up treatment', async () => {
    let counter = 0
    const result = await runBatchEpisode({ ...data, max_turns: 2 }, {
      images: [], simulate: async (kind: string) => kind === 'portrait' ? { opening: '牙齿有白色区域' } : { answer: '不知道', unknown: true },
      consult: async () => ({ status: 'needs_input', next_question: '多久了？', run_id: `r${++counter}` }),
      propose: async () => { throw Error('Budget exhausted cannot be approved') },
    })
    expect(counter).toBe(2)
    expect(result.truncated_dialogue).toBe(true)
    expect(result.proposal.treatment_plan).toBe('')
    expect(JSON.stringify(result.transcript)).toContain('不知道')
  })
})
