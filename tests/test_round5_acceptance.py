"""Frozen v5 learning acceptance with a controlled, source-driven rule calculator.

The calculator never receives evaluator objects. This verifies machinery only;
its 10/10 outputs are not evidence that a real language model learned the rules.
"""
from __future__ import annotations

import hashlib
import json
from pathlib import Path
import re
import tempfile
import unittest

FIXTURE = Path(__file__).parent / "fixtures" / "round5_learning_cases.json"
FIXTURE_SHA256 = "387983983dd92c3c50ddbc772bcddbb5980cfd1ca1973d34dcfddd1b6d8b2fd4"
EVIDENCE = {}


def _array(question):
    return json.loads(re.search(r'\[[^\]]*\]', question).group())


def controlled_answer(source: str, question: str):
    """A deliberately narrow calculator of the ten supplied synthetic protocols.

    Constants and inputs come from learner-visible source/question, never from
    the fixture's expected values. Unsupported protocols fail closed.
    """
    rules = re.findall(r'\[([A-Z]+-\d+\.R\d+)\]', source)
    if not rules:
        return {"status": "unknown", "answer": None, "basis": []}
    protocol = rules[0].split('.')[0]
    if protocol == "LMX-731":
        mapping = dict(re.findall(r'([\u4e00-\u9fff]{2})→([\u4e00-\u9fff]{2})', source))
        answer = [mapping[token] for token in _array(question)]
    elif protocol == "KX-482":
        prices = {name: int(value) for name, value in re.findall(r'每个(.匣)计(\d+)', source)}
        inputs = [(int(n), name) for n, name in re.findall(r'(\d+)个(.匣)', question)]
        threshold = int(re.search(r'严格大于(\d+)', source).group(1))
        discount = int(re.search(r'整单减(\d+)', source).group(1))
        value = sum(n * prices[name] for n, name in inputs)
        value -= discount if sum(n for n, _ in inputs) > threshold else 0
        answer = {"amount": value, "unit": re.search(r'"unit":"([^"]+)"', source).group(1)}
    elif protocol == "QY-956":
        special, special_to = re.search(r'\] (.+?)件一律送(.栈)', source).groups()
        cutoff, high, low = re.search(r'等于(\d+)芽铢送(.栈)；重量小于\d+芽铢送(.栈)', source).groups()
        items = re.findall(r'([紫黑白]纹)[、]?(?:重)?(\d+)芽铢', question)
        results = [special_to if pattern == special else high if int(weight) >= int(cutoff) else low
                   for pattern, weight in items]
        answer = results if '数组' in question else results[0]
    elif protocol == "MY-264":
        asleep = re.search(r'初始状态为(.)', source).group(1)
        awake = re.search(r'事件叩把.变(.)', source).group(1)
        locked = re.search(r'事件封把任意状态变为(.)', source).group(1)
        state, states = asleep, []
        for event in _array(question):
            if event == '封': state = locked
            elif event == '释': state = asleep
            elif state != locked: state = awake if event == '叩' else asleep
            states.append(state)
        answer = states if '数组' in question else state
    elif protocol == "HZ-815":
        values = {char: int(value) for char, value in re.findall(r'([渡禾石])=(\d+)', source)}
        odd, even = map(int, re.search(r'奇数位置权重(\d+)，偶数位置权重(\d+)', source).groups())
        modulus = int(re.search(r'除以(\d+)', source).group(1))
        word = re.search(r'字符串“([^”]+)”', question).group(1)
        answer = sum(values[c] * (odd if i % 2 == 0 else even) for i, c in enumerate(word)) % modulus
    elif protocol == "CW-397":
        order = re.search(r'固定为：(.)在最前，(.)其次，(.)最后', source).groups()
        answer = sorted(_array(question), key=lambda item: (order.index(item[0]), int(item[1:])))
    elif protocol == "TY-628":
        expressions = {tag: (int(mult), int(offset) * (-1 if sign in {'−','-'} else 1))
                       for tag, mult, sign, offset in re.findall(r'带(.)签的输入x换算为(\d+)×x([+−-])(\d+)', source)}
        unit = re.search(r'输出单位为(.)', source).group(1)
        inputs = re.findall(r'(.)签输入([−-]?\d+)', question)
        values = [{"value": expressions[tag][0] * int(n.replace('−','-')) + expressions[tag][1], "unit": unit}
                  for tag, n in inputs]
        answer = values if '数组' in question else values[0]
    elif protocol == "RX-543":
        days = re.search(r'日期循环为：([^，]+)', source).group(1).split('→')[:-1]
        inputs = re.findall(r'从(.日)(?:向前)?推进(\d+)天', question)
        values = [days[(days.index(day) + int(n)) % len(days)] for day, n in inputs]
        answer = values if '数组' in question else values[0]
    elif protocol == "QF-174":
        denied = re.search(r'一律判为(.席)', source).group(1)
        first, second = re.search(r'徽记是(.印)或(.印)', source).groups()
        allowed = re.search(r'判为(.席)；其余', source).group(1)
        inputs = re.findall(r'封页[=＝](是|否)、(?:徽记[=＝])?(.印)、序号[=＝]?(\d+)', question)
        values = [allowed if closed == '否' and badge in {first, second} and int(n) % 2 == 0 else denied
                  for closed, badge, n in inputs]
        answer = values if '数组' in question else values[0]
    elif protocol == "SY-869":
        value = int(re.search(r'初始计数为(\d+)', source).group(1))
        multiple = int(re.search(r'计数乘(\d+)', source).group(1))
        subtract = int(re.search(r'计数减(\d+)', source).group(1))
        for operation in _array(question):
            if operation.startswith('添('): value += int(operation[2:-1])
            elif operation == '映': value *= multiple
            elif operation == '落': value = max(0, value - subtract)
            else: raise ValueError('unsupported operation')
        answer = value
    else:
        raise ValueError('unsupported controlled protocol')
    return {"status": "known", "answer": answer, "basis": rules}


class ControlledLearner:
    def __init__(self, case):
        self.source = case['source']['text']
        self.application = case['learner_visible']['heldout_application']['question']
        self.calls = []

    def chat(self, system, prompt, max_tokens):
        request = json.loads(prompt)
        stage = request['stage']
        combined = str(system) + str(prompt)
        assert not any(key in combined for key in ('evaluator_only', 'expected_answer', 'required_rule_ids', 'expected_status'))
        if stage != 'application':
            assert self.application not in combined, 'heldout question exposed before check submission'
        if stage == 'baseline':
            assert self.source not in combined, 'baseline received source'
            assert 'source' not in request and 'notes' not in request
            answer = {"status": "unknown", "answer": None, "basis": []}
        else:
            supplied = request['source']
            source = supplied if isinstance(supplied, str) else supplied.get('text', supplied.get('content', ''))
            assert self.source == source or self.source in json.dumps(supplied, ensure_ascii=False)
            answer = {"notes": source} if stage == 'notes' else controlled_answer(source, request['question'])
        self.calls.append({"stage": stage, "request": request, "output": answer})
        return json.dumps(answer, ensure_ascii=False)


class FrozenLearningFixtureTest(unittest.TestCase):
    def test_fixture_is_frozen_and_calculator_does_not_use_scoring_values(self):
        self.assertEqual(hashlib.sha256(FIXTURE.read_bytes()).hexdigest(), FIXTURE_SHA256)
        data = json.loads(FIXTURE.read_text(encoding='utf-8'))
        self.assertEqual(data['status'], 'FROZEN')
        self.assertEqual(len(data['tasks']), 10)
        for case in data['tasks']:
            for learner_stage, grading_stage in [('learning_after', 'after'), ('heldout_application', 'heldout')]:
                with self.subTest(case=case['id'], stage=learner_stage):
                    actual = controlled_answer(case['source']['text'], case['learner_visible'][learner_stage]['question'])
                    rubric = case['evaluator_only'][grading_stage]
                    self.assertEqual(actual['answer'], rubric['expected_answer'])
                    self.assertTrue(set(rubric['required_rule_ids']).issubset(actual['basis']))

class LearningLifecycleAcceptanceTest(unittest.IsolatedAsyncioTestCase):
    async def test_ten_frozen_groups_first_answers_phase_isolation_restart_and_evidence(self):
        from suwan.bus import EventBus
        from suwan.clock import ManualClock
        from suwan.organs.memory import MemoryOrgan
        from suwan.services.learning import LearningService
        cases = json.loads(FIXTURE.read_text(encoding='utf-8'))['tasks']
        reports = []
        with tempfile.TemporaryDirectory(prefix='suwan-v5-fixed-learning-') as temporary:
            for case in cases:
                with self.subTest(case=case['id']):
                    folder = Path(temporary) / case['id']
                    folder.mkdir()
                    clock = ManualClock()
                    learner = ControlledLearner(case)
                    def boot():
                        bus = EventBus()
                        memory = MemoryOrgan(bus, clock, file_path=folder / 'memory.json')
                        memory._load()
                        service = LearningService(bus, clock, memory=memory, data_dir=folder,
                                                  llm=learner, api_enabled=True)
                        return memory, service
                    memory, service = boot()
                    source = memory.add(case['source']['text'], kind='knowledge', source='manual', persist=True)
                    visible = case['learner_visible']
                    task = await service.create({'title': case['title'], 'source_id': source.id,
                        'question': visible['learning_before']['question'],
                        'application_question': visible['heldout_application']['question'],
                        'instruction': visible['learning_instruction'], 'concept_key': case['source']['id'],
                        'evaluator': {key: case['evaluator_only'][key] for key in ('after', 'heldout')}})
                    identifier, progression = task['id'], [0]
                    task = await service.advance(identifier)
                    self.assertEqual(task['baseline']['status'], 'unknown')
                    self.assertIsNone(task['baseline']['answer'])
                    self.assertFalse(task['baseline']['correct'])
                    progression.append(task['progress']['completed'])
                    task = await service.advance(identifier)
                    self.assertTrue(task['notes'])
                    progression.append(task['progress']['completed'])
                    self.assertEqual(service.retrieve(case['source']['id']), [])
                    # Restart before grading: the persistent note is still unverified.
                    memory, service = boot()
                    await service.initialise()
                    self.assertEqual(service.view()['tasks'][0]['stage'], 'check')
                    self.assertEqual(service.retrieve(case['source']['id']), [])
                    task = await service.advance(identifier)
                    self.assertTrue(task['check']['correct'])
                    progression.append(task['progress']['completed'])
                    entries = service.retrieve(case['source']['id'])
                    self.assertEqual(len(entries), 1)
                    self.assertEqual(entries[0]['citations'][0]['source_id'], source.id)
                    self.assertTrue(set(case['evaluator_only']['after']['required_rule_ids']).issubset(
                        entries[0]['citations'][0]['rule_ids']))
                    # Restart after the check but before first exposure of heldout question.
                    memory, service = boot()
                    await service.initialise()
                    self.assertEqual(service.view()['tasks'][0]['stage'], 'application')
                    task = await service.advance(identifier)
                    self.assertEqual(task['status'], 'completed')
                    self.assertTrue(task['application']['correct'])
                    self.assertFalse(task['application']['reused'])
                    self.assertTrue(task['first_improvement'])
                    progression.append(task['progress']['completed'])
                    self.assertEqual(progression, [0, 0, 0, 1, 2])
                    self.assertEqual(service.goals()[0]['progress'], 2)
                    self.assertEqual([call['stage'] for call in learner.calls],
                                     ['baseline', 'notes', 'check', 'application'])
                    memory, service = boot()
                    await service.initialise()
                    self.assertEqual((await service.advance(identifier))['status'], 'completed')
                    self.assertEqual(len(learner.calls), 4)
                    retrieved = service.retrieve(case['source']['id'])[0]
                    # Repeat application from freshly loaded, cited notes, not the scoring object.
                    reanswered = controlled_answer(retrieved['text'], visible['heldout_application']['question'])
                    self.assertEqual(reanswered['answer'], task['application']['answer'])
                    reports.append({'id': case['id'], 'task_id': identifier, 'source_id': source.id,
                        'baseline_first_answer': learner.calls[0]['output'],
                        'check_first_answer': learner.calls[2]['output'],
                        'application_first_answer': learner.calls[3]['output'],
                        'check_passed': task['check']['correct'],
                        'application_passed': task['application']['correct'],
                        'progression': progression, 'calls': len(learner.calls),
                        'restart_before_check': True, 'restart_before_application': True,
                        'restart_retrieval_passed': True, 'cited_source_ids': [source.id],
                        'controlled_improvement': task['first_improvement']})
        EVIDENCE['tasks'] = reports
        EVIDENCE['controlled_counts'] = {'tasks': len(reports), 'check_passed': sum(r['check_passed'] for r in reports),
            'application_passed': sum(r['application_passed'] for r in reports),
            'controlled_improvement': sum(r['controlled_improvement'] for r in reports)}
        EVIDENCE['real_model_quality'] = {'status': 'NOT_RUN', 'after_8_of_10': None, 'growth_5_of_10': None}

    async def test_forget_and_correction_remove_knowledge_answers_and_goal_evidence_after_restart(self):
        from suwan.bus import EventBus
        from suwan.clock import ManualClock
        from suwan.organs.memory import MemoryOrgan
        from suwan.services.learning import LearningService
        case = json.loads(FIXTURE.read_text(encoding='utf-8'))['tasks'][0]
        results = []
        with tempfile.TemporaryDirectory(prefix='suwan-v5-learning-withdraw-') as temporary:
            for mode in ('forget', 'correct'):
                folder = Path(temporary) / mode
                folder.mkdir()
                bus, clock = EventBus(), ManualClock()
                memory = MemoryOrgan(bus, clock, file_path=folder / 'memory.json')
                memory._load()
                source = memory.add(case['source']['text'], kind='knowledge', source='manual', persist=True)
                service = LearningService(bus, clock, memory=memory, data_dir=folder,
                                          llm=ControlledLearner(case), api_enabled=True)
                task = await service.create({'title': case['title'], 'source_id': source.id,
                    'question': case['learner_visible']['learning_before']['question'],
                    'application_question': case['learner_visible']['heldout_application']['question'],
                    'evaluator': {key: case['evaluator_only'][key] for key in ('after', 'heldout')}})
                for _ in range(4):
                    task = await service.advance(task['id'])
                self.assertEqual(task['progress']['completed'], 2)
                if mode == 'forget': memory.delete(source.id)
                else: memory.correct(source.id, '[LMX-731.R1] 原规则已更正，不能继续按旧映射回答。')
                view = service.view()
                self.assertEqual(view['entries'], [])
                self.assertEqual(view['tasks'][0]['status'], 'withdrawn')
                self.assertEqual(view['tasks'][0]['notes'], '')
                self.assertIsNone(view['tasks'][0]['check'])
                self.assertEqual(service.goals()[0]['progress'], 0)
                fresh_memory = MemoryOrgan(bus, clock, file_path=folder / 'memory.json')
                fresh_memory._load()
                fresh = LearningService(bus, clock, memory=fresh_memory, data_dir=folder,
                                        llm=ControlledLearner(case), api_enabled=True)
                await fresh.initialise()
                self.assertEqual(fresh.retrieve('LMX-731'), [])
                self.assertEqual(fresh.goals()[0]['progress'], 0)
                self.assertNotIn('玻籽', (folder / 'learning-v5.json').read_text(encoding='utf-8'))
                results.append({'mode': mode, 'withdrawn': True, 'restart_no_old_knowledge': True, 'goal_evidence': 0})
        EVIDENCE['privacy'] = results


if __name__ == '__main__':
    unittest.main()
