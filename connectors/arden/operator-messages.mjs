import { randomUUID } from "node:crypto"

export class OperatorMessages {
  constructor(arden, sessionId, epochMs, evidence, worldTools = []) {
    this.arden = arden
    this.sessionId = sessionId
    this.epochMs = epochMs
    this.evidence = evidence
    this.worldTools = worldTools
    this.submitted = 0
    this.pendingRuns = new Set()
    this.reportedQuestions = new Set()
  }

  /** Actionable native questions not yet reported, each as the report text the runner hands to
   *  the operator: the prompt, its detail and its choices. The operator's next message answers
   *  the question through `submit`. */
  async newQuestionReports(time) {
    const reports = []
    for (const question of await this.actionableQuestions()) {
      if (this.reportedQuestions.has(question.question_id)) continue
      this.reportedQuestions.add(question.question_id)
      const content = question.content ?? {}
      const choices = (content.choices ?? []).map((choice) => choice.label)
      const message = [
        content.prompt,
        content.detail,
        choices.length ? `Choices: ${choices.join("; ")}` : undefined
      ].filter((part) => typeof part === "string" && part.trim()).join("\n\n")
      if (!message) continue
      this.evidence.write("native_question_reported", {
        question_id: question.question_id,
        asking_run_id: question.run_id,
        time
      })
      reports.push(message)
    }
    return reports
  }

  async actionableQuestions() {
    const questions = []
    let cursor
    do {
      const query = new URLSearchParams({ state: "actionable" })
      if (cursor !== undefined) query.set("cursor", cursor)
      const page = await this.arden.request("GET", `/chat/questions?${query}`)
      questions.push(...page.questions)
      cursor = page.next_cursor ?? undefined
    } while (cursor !== undefined)
    return questions
  }

  messageText(entry, time, observation) {
    const context = { ...observation }
    delete context.messages
    const preamble = this.submitted === 0
      ? `This is an isolated task. Use these connected MCP tools for simulated service data: ${this.worldTools.join(", ") || "none"}. Native mail, calendar and other real service tools do not reach this world. Use Arden's native Automations for delayed follow-up and native ask_user for operator questions; these remain available in scheduled workers. World MCP bindings are already configured and inherited by native Automations. Use those existing bindings; do not add redundant world MCP grants when creating an Automation. Check native tool results for errors and verify creation before claiming it succeeded. Current simulated time: ${new Date(this.epochMs + time * 1000).toISOString()}.\n\n`
      : ""
    return `${preamble}${entry.message}\n\nCurrent filtered world observation:\n${JSON.stringify(context)}`
  }

  async answerQuestion(question, entry, time) {
    const answered = await this.arden.request(
      "POST",
      `/chat/questions/${encodeURIComponent(question.question_id)}/answer`,
      {
        client_id: randomUUID(),
        answer: { kind: "text", text: entry.message }
      }
    )
    if (answered.continuation?.run_id) this.pendingRuns.add(answered.continuation.run_id)
    this.evidence.write("native_question_answer", {
      question_id: question.question_id,
      asking_run_id: question.run_id,
      asking_session_id: question.session_id,
      destination_session_id: question.destination_session_id,
      continuation: answered.continuation,
      text: entry.message,
      time
    })
  }

  async submitMessage(entry, time, observation) {
    const submitted = await this.arden.client.submit({
      sessionId: this.sessionId,
      message: this.messageText(entry, time, observation),
      approvalMode: "allow"
    })
    this.pendingRuns.add(submitted.runId)
    this.evidence.write("operator_message", {
      run_id: submitted.runId,
      session_id: this.sessionId,
      time,
      message: entry.message
    })
  }

  async submit(messages, time, observation = {}) {
    for (; this.submitted < messages.length; this.submitted += 1) {
      const entry = messages[this.submitted]
      const questions = this.submitted === 0 ? [] : await this.actionableQuestions()
      if (questions.length > 1)
        throw new Error(
          `Operator message has ${questions.length} actionable Arden questions; explicit routing is required`
        )
      if (questions.length === 1) {
        await this.answerQuestion(questions[0], entry, time)
        continue
      }
      await this.submitMessage(entry, time, observation)
    }
  }
}
