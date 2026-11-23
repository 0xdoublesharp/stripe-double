"""Synthetic payout balances, reservations and lifecycle over HTTP."""
from concurrent.futures import ThreadPoolExecutor
import http.client
import json
import threading
import unittest
from unittest.mock import patch
import test_near_term
from stripe_double.state import new_id


class PayoutTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control
    account = test_near_term.NearTermHttpTests.account
    admin = test_near_term.NearTermHttpTests.admin
    ready = test_near_term.NearTermHttpTests.ready
    restart = test_near_term.test_http.HttpTests.restart

    def test_standard_account_exposes_schedule_without_account_write(self):
        account = self.account()
        path = '/v1/accounts/' + account
        status, profile, _ = self.request('GET', path)
        self.assertEqual(status, 200)
        self.assertEqual(profile.get('settings', {}).get('payouts', {}).get('schedule'),
                         {'interval':'daily','delay_days':2})
        self.assertEqual(self.request('POST', path,
            {'settings[payouts][schedule][interval]':'manual'}, key='schedule')[0], 400)

    def test_filter_reconciled_transactions_by_connected_payout(self):
        account, other = self.account('owner'), self.account('foreign')
        payout = new_id('po')
        charge = new_id('ch')
        payment = new_id('py')
        with self.store.transaction():
            self.store._put(self.run, account, 'payout', {'id':payout,'object':'payout',
                'amount':500,'created':10,'automatic':True,'reconciliation_status':'completed'})
            for owner, kind, source, related in [(account,'charge',charge,True),
                (account,'payment',payment,True), (other,'charge',new_id('ch'),True),
                (account,'charge',new_id('ch'),False)]:
                txn = {'id':new_id('txn'),'object':'balance_transaction','created':10,
                       'type':kind,'source':source}
                if related:
                    txn['local_automatic_payout'] = payout
                self.store._put(self.run, owner, 'balance_transaction', txn)
        route = '/v1/balance_transactions?payout=' + payout + '&limit=1'
        status, first, _ = self.request('GET', route, account=account)
        self.assertEqual(status, 200)
        self.assertTrue(first['has_more'])
        status, last, _ = self.request('GET', route+'&starting_after='+first['data'][0]['id'], account=account)
        self.assertEqual(status, 200)
        self.assertFalse(last['has_more'])
        self.assertEqual({t['source'] for t in first['data']+last['data']}, {charge,payment})
        self.assertTrue(all('local_automatic_payout' not in t for t in first['data']+last['data']))
        self.assertEqual(self.request('GET', route, account=other)[0], 403)

    def fund(self, account, amount):
        return self.control('balances/fund', {'account':account,'amount':str(amount),
            'expected_state_version':self.store.run(self.run)['state_version']})

    def test_payout_reservation_failure_and_account_isolation(self):
        first, second = self.account('first'), self.account('second')
        self.ready()
        self.assertEqual(self.fund(first,1000)[0],200)
        status, balance, _ = self.request('GET','/v1/balance',account=first)
        self.assertEqual(status,200,balance)
        self.assertEqual(balance['available'][0]['amount'],1000)
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'600','currency':'usd'},account=first,key='payout')
        self.assertEqual(status,200,payout)
        self.assertEqual(payout['status'],'pending')
        self.assertEqual(self.request('POST','/v1/payouts',{'amount':'600','currency':'usd'},account=first,key='payout')[1],payout)
        self.assertEqual(self.request('GET','/v1/balance',account=first)[1]['available'][0]['amount'],400)
        self.assertNotEqual(self.request('POST','/v1/payouts',{'amount':'401','currency':'usd'},account=first,key='excess')[0],200)
        self.assertNotEqual(self.request('GET','/v1/payouts/' + payout['id'],account=second)[0],200)
        info = self.control('resources/inspect',{'kind':'payout','resource_id':payout['id'],'account':first})[1]
        status, result, _ = self.control('payouts/' + payout['id'] + '/actions',
            {'action':'fail','account':first,'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,result)
        self.assertEqual(self.request('GET','/v1/balance',account=first)[1]['available'][0]['amount'],1000)
        self.assertEqual(self.request('GET','/v1/payouts/' + payout['id'],account=first)[1]['status'],'failed')
        self.assertEqual(self.request('GET','/v1/balance',account=second)[1]['available'][0]['amount'],0)
        types = [json.loads(bytes(row['payload']))['type'] for row in self.store.events(self.run)]
        self.assertIn('payout.created',types)
        self.assertIn('payout.failed',types)

    def test_payout_paid_and_cancel_are_durable(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        payout = self.request('POST','/v1/payouts',{'amount':'300','currency':'usd'},account=account,key='paid')[1]
        info = self.control('resources/inspect',{'kind':'payout','resource_id':payout['id'],'account':account})[1]
        action = {'action':'pay','account':account,'expected_provider_version':info['provider_version']}
        self.assertEqual(self.control('payouts/' + payout['id'] + '/actions',action)[0],200)
        self.assertEqual(self.control('payouts/' + payout['id'] + '/actions',action)[0],409)
        pending = self.request('POST','/v1/payouts',{'amount':'400','currency':'usd'},account=account,key='cancel-me')[1]
        cancel_path = '/v1/payouts/' + pending['id'] + '/cancel'
        status, canceled, _ = self.request('POST',cancel_path,account=account,key='cancel')
        self.assertEqual(status,200,canceled)
        self.assertEqual(canceled['status'],'canceled')
        self.restart()
        self.assertEqual(self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount'],700)
        self.assertEqual(self.request('GET','/v1/payouts/' + payout['id'],account=account)[1]['status'],'paid')
        self.assertEqual(self.request('POST',cancel_path,account=account,key='cancel')[1],canceled)
        page = self.request('GET','/v1/payouts?limit=10',account=account)[1]
        self.assertEqual({p['id'] for p in page['data']},{payout['id'],pending['id']})

    @patch('stripe_double.state.MAX_BALANCE',99999999)
    def test_pending_reservations_preserve_capacity_to_release(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,99999999)[0],200)
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'100','currency':'usd'},account=account,key='reserve')
        self.assertEqual(status,200,payout)
        self.assertEqual(self.fund(account,1)[0],429)
        self.assertEqual(self.request('POST','/v1/payouts/' + payout['id'] + '/cancel',account=account,key='return')[0],200)
        self.assertEqual(self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount'],99999999)

    @patch('stripe_double.wire.MAX_EVENTS',4)
    def test_outbox_capacity_cannot_strand_a_pending_payout(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        status, payout, _ = self.request('POST','/v1/payouts',{'amount':'400','currency':'usd'},account=account,key='reserved')
        self.assertEqual(status,200,payout)
        self.assertEqual(self.admin('duplicate_last_event')[0],429)
        status, canceled, _ = self.request('POST','/v1/payouts/'+payout['id']+'/cancel',account=account,key='cancel')
        self.assertEqual(status,200,canceled)
        self.assertEqual(canceled['status'],'canceled')
        self.assertEqual(self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount'],1000)

    def destination(self, account, action='replace'):
        status, configured, _ = self.control('payout_destinations/configure',{'account':account,'action':action,
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,configured)
        return configured['destination']

    def quote(self, account, key='quote', gross=600):
        status, quote, _ = self.request('POST','/v1/payouts/instant_quotes',{'currency':'usd','gross':str(gross)},
                                        account=account,key=key)
        self.assertEqual(status,200,quote)
        return quote

    def instant_form(self, quote):
        return {'method':'instant','quote':quote['id'],'amount':str(quote['net']),
                **{field:str(quote[field]) for field in ('currency','gross','fee','destination','available','expires_at')}}

    def available(self, account):
        return self.request('GET','/v1/balance',account=account)[1]['available'][0]['amount']

    def assert_refused(self, response, status, code):
        self.assertEqual((response[0],response[1].get('error',{}).get('code')),(status,code),response[1])

    def test_instant_quote_is_scoped_and_binds_every_execution_term(self):
        account, foreign = self.account(), self.account('foreign')
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        request = {'currency':'usd','gross':'600'}
        self.assert_refused(self.request('POST','/v1/payouts/instant_quotes',request,account=account,key='no-destination'),
                            409,'simulator_payout_destination_unavailable')
        destination = self.destination(account)
        self.assert_refused(self.request('POST','/v1/payouts/instant_quotes',{**request,'gross':'1001'},
                                         account=account,key='insufficient'),409,'simulator_insufficient_funds')
        quote = self.quote(account)
        self.assertEqual((quote['account'],quote['currency'],quote['gross'],quote['fee'],quote['net']),
                         (account,'usd',600,9,591))
        self.assertEqual((quote['destination'],quote['available'],quote['expires_at'],quote['payout']),
                         (destination,1000,int(self.clock())+300,None))
        self.assertEqual(self.request('GET','/v1/payouts/instant_quotes/' + quote['id'],account=foreign)[0],403)
        form = self.instant_form(quote)
        self.assert_refused(self.request('POST','/v1/payouts', form,account=foreign,key='foreign'),403,'simulator_resource')
        for field, wrong in {'currency':'eur','gross':'601','fee':'10','amount':'590','destination':new_id('card'),
                             'available':'999','expires_at':str(quote['expires_at']+1)}.items():
            with self.subTest(field=field):
                self.assert_refused(self.request('POST','/v1/payouts',{**form,field:wrong},account=account,key='wrong-' + field),
                                    409,'simulator_payout_quote_mismatch')
        self.assertEqual(self.available(account),1000)
        self.assertEqual(self.request('GET','/v1/payouts',account=account)[1]['data'],[])
        status, payout, _ = self.request('POST','/v1/payouts',form,account=account,key='valid')
        self.assertEqual(status,200,payout)
        self.assertEqual((payout['amount'],payout['gross'],payout['fee'],payout['method'],payout['destination']),
                         (591,600,9,'instant',destination))
        self.assertEqual(self.available(account),400)
        info = self.control('resources/inspect',{'kind':'payout','resource_id':payout['id'],'account':account})[1]
        status, paid, _ = self.control('payouts/' + payout['id'] + '/actions',
            {'action':'pay','account':account,'expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,paid)
        self.assertEqual(paid['status'],'paid')
        self.assertEqual(self.request('GET','/v1/payouts/' + payout['id'],account=account)[1]['arrival_date'],int(self.clock()))
        self.assertEqual(self.available(account),400)

        cancellation_quote = self.quote(account,key='cancel-quote',gross=300)
        status, pending, _ = self.request('POST','/v1/payouts',self.instant_form(cancellation_quote),
                                          account=account,key='cancel-payout')
        self.assertEqual(status,200,pending)
        status, canceled, _ = self.request('POST','/v1/payouts/' + pending['id'] + '/cancel',account=account,key='cancel')
        self.assertEqual(status,200,canceled)
        self.assertEqual(canceled['status'],'canceled')
        self.assertEqual(self.available(account),400)

    def test_instant_quote_rejects_expiry_destination_and_balance_changes(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        old_destination = self.destination(account)

        def submit(quote, key, code):
            self.assert_refused(self.request('POST','/v1/payouts',self.instant_form(quote),account=account,key=key),409,code)
            canonical = self.request('GET','/v1/payouts/instant_quotes/' + quote['id'],account=account)[1]
            self.assertIsNone(canonical['payout'])

        quote = self.quote(account,key='expiring')
        self.clock.now = quote['expires_at']
        submit(quote,'expired','simulator_payout_quote_expired')
        quote = self.quote(account,key='old-destination')
        self.assertNotEqual(self.destination(account),old_destination)
        submit(quote,'old-destination','simulator_payout_destination_changed')
        quote = self.quote(account,key='disabled-destination')
        self.assertIsNone(self.destination(account,'disable'))
        submit(quote,'disabled-destination','simulator_payout_destination_changed')
        self.destination(account)
        quote = self.quote(account,key='before-balance-change')
        standard = self.request('POST','/v1/payouts',{'amount':'100','currency':'usd'},account=account,key='standard')[1]
        submit(quote,'lower-available','simulator_payout_quote_stale')
        self.assertEqual(self.request('POST','/v1/payouts/' + standard['id'] + '/cancel',account=account,key='cancel')[0],200)
        submit(quote,'restored-balance','simulator_payout_quote_stale')
        self.assertEqual(self.available(account),1000)
        self.assertEqual(len(self.request('GET','/v1/payouts',account=account)[1]['data']),1)

    def test_instant_execution_serializes_same_and_different_keys(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,3000)[0],200)
        self.destination(account)

        def race(form, keys):
            barrier = threading.Barrier(2)
            def execute(key):
                barrier.wait(timeout=2)
                return self.request('POST','/v1/payouts',form,account=account,key=key)
            with ThreadPoolExecutor(max_workers=2) as pool:
                return list(pool.map(execute,keys))

        same_key = race(self.instant_form(self.quote(account,key='same-quote')),('same','same'))
        self.assertEqual([response[0] for response in same_key],[200,200])
        self.assertEqual(same_key[0][1],same_key[1][1])
        different_keys = race(self.instant_form(self.quote(account,key='other-quote')),('first','second'))
        self.assertEqual(sorted(response[0] for response in different_keys),[200,409])
        self.assert_refused(next(response for response in different_keys if response[0] == 409),409,'simulator_payout_quote_consumed')
        self.assertEqual(self.available(account),1800)
        self.assertEqual(len(self.request('GET','/v1/payouts',account=account)[1]['data']),2)
        events = [json.loads(row['payload']) for row in self.store.events(self.run)]
        self.assertEqual(sum(event['type'] == 'payout.created' for event in events),2)

    @patch('stripe_double.state.MAX_ACCOUNTING_OBJECTS',3)
    def test_instant_execution_recovers_dropped_replies_and_failure_credits_gross_once(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        self.destination(account)
        self.assertEqual(self.admin('drop_next_reply_after_commit')[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.quote(account)
        self.restart()
        quote = self.quote(account)
        self.assertEqual(len(self.store.objects(self.run,'payout_quote')),1)
        form = self.instant_form(quote)
        self.assertEqual(self.admin('drop_next_reply_after_commit')[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request('POST','/v1/payouts',form,account=account,key='execute')
        self.restart()
        quote_read = self.request('GET','/v1/payouts/instant_quotes/' + quote['id'],account=account)[1]
        status, payout, _ = self.request('POST','/v1/payouts',form,account=account,key='execute')
        self.assertEqual(status,200,payout)
        self.assertEqual(quote_read['payout'],payout['id'])
        self.assertEqual(payout['status'],'pending')
        self.assertEqual(self.available(account),400)
        self.assert_refused(self.request('POST','/v1/payouts',{**form,'fee':'10'},account=account,key='execute'),
                            409,'simulator_idempotency')
        debit = self.request('GET','/v1/balance_transactions/' + payout['balance_transaction'],account=account)[1]
        self.assertEqual((debit['source'],debit['amount'],debit['fee'],debit['net']),(payout['id'],-591,9,-600))

        self.assertEqual(self.control('balances/configure',{'account':account,'fee_bps':'0','fee_flat':'0',
            'settlement_delay':'0','expected_state_version':self.store.run(self.run)['state_version']})[0],429)
        version = self.control('resources/inspect',{'kind':'payout','resource_id':payout['id'],'account':account})[1]['provider_version']
        failure = {'action':'fail','account':account,'expected_provider_version':version}
        endpoint = 'payouts/' + payout['id'] + '/actions'
        self.assertEqual(self.control(endpoint,failure)[0],200)
        self.assertEqual(self.control(endpoint,failure)[0],409)
        failed = self.request('GET','/v1/payouts/' + payout['id'],account=account)[1]
        self.assertEqual((failed['status'],failed['failure_code']),('failed','account_closed'))
        credit = self.request('GET','/v1/balance_transactions/' + failed['failure_balance_transaction'],account=account)[1]
        self.assertEqual((credit['source'],credit['type'],credit['net']),(payout['id'],'payout_failure',600))
        self.assertEqual(self.available(account),1000)
        self.assertEqual(self.request('POST','/v1/payouts',form,account=account,key='execute')[1],payout)
        events = [json.loads(row['payload']) for row in self.store.events(self.run)]
        self.assertEqual([event['type'] for event in events if event['data']['object']['id'] == payout['id']],
                         ['payout.created','payout.failed'])

    def test_instant_execution_rolls_back_if_event_or_receipt_cannot_commit(self):
        account = self.account()
        self.ready()
        self.assertEqual(self.fund(account,1000)[0],200)
        self.destination(account)
        quote = self.quote(account)
        form = self.instant_form(quote)
        event_count = len(self.store.events(self.run))

        def execute():
            return self.request('POST','/v1/payouts',form,account=account,key='execute')

        def assert_rolled_back():
            self.assertEqual(self.available(account),1000)
            self.assertEqual(self.request('GET','/v1/payouts',account=account)[1]['data'],[])
            self.assertEqual(self.request('GET','/v1/payouts/instant_quotes/' + quote['id'],account=account)[1],quote)
            self.assertEqual(self.store.objects(self.run,'balance_transaction'),[])
            self.assertEqual(len(self.store.events(self.run)),event_count)

        with patch('stripe_double.wire.MAX_EVENTS',event_count+1):
            self.assert_refused(execute(),429,'simulator_limit')
        assert_rolled_back()
        with self.store.transaction():
            self.store.connection.execute("CREATE TEMP TRIGGER refuse_receipt BEFORE INSERT ON idempotency "
                                          "BEGIN SELECT RAISE(ABORT, 'synthetic receipt failure'); END")
        self.assert_refused(execute(),503,'simulator_state')
        assert_rolled_back()
        with self.store.transaction():
            self.store.connection.execute('DROP TRIGGER refuse_receipt')
        self.assertEqual(execute()[0],200)
        self.assertEqual(self.available(account),400)
