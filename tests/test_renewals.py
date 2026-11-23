"""Synthetic subscription renewals and scoped saved-card captures."""
from datetime import datetime, timezone
import http.client
import json
import unittest
from unittest.mock import patch
import test_near_term
from stripe_double.state import new_id


class RenewalTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control

    def seed(self):
        self.clock.now = datetime(2026,1,31,10,tzinfo=timezone.utc).timestamp()
        status, seeded, _ = self.control('billing/subscriptions/seed',{'amount':'1200','interval':'month',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,seeded)
        self.clock.now = datetime(2026,2,28,10,tzinfo=timezone.utc).timestamp()
        return seeded

    def action(self, sub, action, version):
        return self.control('billing/subscriptions/' + sub + '/actions',
            {'action':action,'expected_provider_version':version})

    def info(self, sub):
        return self.control('resources/inspect',{'kind':'subscription','resource_id':sub})[1]

    def test_paid_renewal_creates_one_linked_invoice_and_keeps_calendar_anchor(self):
        seed = self.seed()
        info = self.info(seed['subscription'])
        status, result, _ = self.action(seed['subscription'],'renew',info['provider_version'])
        self.assertEqual(status,200,result)
        self.assertEqual(result['status'],'active')
        self.assertEqual(self.action(seed['subscription'],'renew',info['provider_version'])[0],409)
        sub = self.request('GET','/v1/subscriptions/' + seed['subscription'])[1]
        self.assertEqual(sub['current_period_end'],int(datetime(2026,3,31,10,tzinfo=timezone.utc).timestamp()))
        invoices = self.request('GET','/v1/invoices?subscription=' + sub['id'])[1]['data']
        self.assertEqual(len(invoices),2)
        renewal = next(i for i in invoices if i['id'] == result['invoice'])
        self.assertEqual((renewal['status'],renewal['amount_paid']),('paid',1200))
        payments = self.request('GET','/v1/invoice_payments?invoice=' + renewal['id'])[1]['data']
        self.assertEqual(len(payments),1)
        self.assertEqual(payments[0]['amount_paid'],1200)

    def test_failed_renewal_retries_existing_invoice_explicitly(self):
        seed = self.seed()
        info = self.info(seed['subscription'])
        status, failed, _ = self.action(seed['subscription'],'renew_fail',info['provider_version'])
        self.assertEqual(status,200,failed)
        self.assertEqual(failed['status'],'past_due')
        invoice = self.request('GET','/v1/invoices/' + failed['invoice'])[1]
        self.assertEqual((invoice['status'],invoice['amount_paid']),('open',0))
        self.assertEqual(self.action(seed['subscription'],'cancel',failed['provider_version'])[0],409)
        status, recovered, _ = self.action(seed['subscription'],'retry_renewal',failed['provider_version'])
        self.assertEqual(status,200,recovered)
        self.assertEqual((recovered['status'],recovered['invoice']),('active',invoice['id']))
        self.assertEqual(self.request('GET','/v1/invoices/' + invoice['id'])[1]['status'],'paid')
        self.assertEqual(len(self.request('GET','/v1/invoices?subscription=' + seed['subscription'])[1]['data']),2)

    def test_once_promotion_discounts_checkout_but_not_renewal(self):
        seed = self.seed()
        status, promotion, _ = self.control('billing/promotions/seed',{'code':'LOCAL_SAVE25','percent_off':'25',
            'duration':'once','expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,promotion)
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'discounts[0][promotion_code]':promotion['promotion_code'],
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='promotion')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id']})[1]
        status, completed, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,completed)
        checkout = self.request('GET','/v1/checkout/sessions/' + session['id'])[1]
        self.assertEqual((checkout['amount_subtotal'],checkout['total_details']['amount_discount'],checkout['amount_total']),(1200,300,900))
        status, expanded, _ = self.request('GET','/v1/checkout/sessions/' + session['id'] + '?expand%5B%5D=discounts')
        self.assertEqual(status,200,expanded)
        self.assertEqual(expanded['discounts'][0]['promotion_code'],promotion['promotion_code'])
        sub = self.request('GET','/v1/subscriptions/' + completed['subscription'])[1]
        expanded_sub = self.request('GET','/v1/subscriptions/' + sub['id'] + '?expand%5B%5D=discounts')[1]
        self.assertEqual(expanded_sub['discounts'][0]['promotion_code'],promotion['promotion_code'])
        first_invoice = self.request('GET','/v1/invoices?subscription=' + sub['id'])[1]['data'][0]
        expanded_invoice = self.request('GET','/v1/invoices/' + first_invoice['id'] + '?expand%5B%5D=discounts')[1]
        self.assertEqual(expanded_invoice['discounts'][0]['promotion_code'],promotion['promotion_code'])
        self.assertEqual(expanded_invoice['total_discount_amounts'][0]['amount'],300)
        self.clock.now = sub['current_period_end']
        version = self.info(sub['id'])['provider_version']
        status, renewal, _ = self.action(sub['id'],'renew',version)
        self.assertEqual(status,200,renewal)
        self.assertEqual(self.request('GET','/v1/invoices/' + renewal['invoice'])[1]['amount_paid'],1200)
        updated = self.request('GET','/v1/subscriptions/' + sub['id'] + '?expand%5B%5D=discounts')[1]
        self.assertEqual(updated['discounts'],[])

    def test_forever_promotion_is_attributed_on_renewal_invoice(self):
        seed = self.seed()
        status, promo, _ = self.control('billing/promotions/seed',{'code':'LOCAL_FOREVER','percent_off':'25',
            'duration':'forever','expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,200,promo)
        form = {'mode':'subscription','customer':seed['customer'],
                'line_items[0][price]':seed['price'],'line_items[0][quantity]':'1',
                'discounts[0][promotion_code]':promo['promotion_code'],
                'success_url':'http://127.0.0.1/success','cancel_url':'http://127.0.0.1/cancel'}
        status, session, _ = self.request('POST','/v1/checkout/sessions',form,key='forever')
        self.assertEqual(status,200,session)
        info = self.control('resources/inspect',{'kind':'checkout_session','resource_id':session['id']})[1]
        status, completed, _ = self.control('checkout/sessions/' + session['id'] + '/actions',
            {'action':'complete','expected_provider_version':info['provider_version']})
        self.assertEqual(status,200,completed)
        sub = self.request('GET','/v1/subscriptions/' + completed['subscription'])[1]
        self.clock.now = sub['current_period_end']
        status, renewed, _ = self.action(sub['id'],'renew',self.info(sub['id'])['provider_version'])
        self.assertEqual(status,200,renewed)
        status, invoice, _ = self.request('GET','/v1/invoices/' + renewed['invoice'] + '?expand%5B%5D=discounts')
        self.assertEqual(status,200,invoice)
        self.assertEqual(invoice['amount_paid'],900)
        self.assertEqual(invoice['total_discount_amounts'][0]['amount'],300)
        self.assertEqual(invoice['discounts'][0]['promotion_code'],promo['promotion_code'])

    @patch('stripe_double.wire.MAX_EVENTS',10)
    def test_outbox_capacity_reserves_room_to_recover_failed_renewal(self):
        seed = self.seed()
        status, failed, _ = self.action(seed['subscription'],'renew_fail',self.info(seed['subscription'])['provider_version'])
        self.assertEqual(status,200,failed)
        status, _, _ = self.control('actions',{'action':'duplicate_last_event',
            'expected_state_version':self.store.run(self.run)['state_version']})
        self.assertEqual(status,429)
        status, recovered, _ = self.action(seed['subscription'],'retry_renewal',failed['provider_version'])
        self.assertEqual(status,200,recovered)
        self.assertEqual(recovered['status'],'active')


class SavedCardTests(unittest.TestCase):
    setUp = test_near_term.NearTermHttpTests.setUp
    close = test_near_term.NearTermHttpTests.close
    request = test_near_term.NearTermHttpTests.request
    control = test_near_term.NearTermHttpTests.control
    account = test_near_term.NearTermHttpTests.account
    ready = test_near_term.NearTermHttpTests.ready
    exchange = test_near_term.NearTermHttpTests.exchange
    component = test_near_term.NearTermHttpTests.component
    grant = test_near_term.NearTermHttpTests.grant
    admin = test_near_term.NearTermHttpTests.admin
    restart = test_near_term.test_http.HttpTests.restart

    def customer(self, account, key='customer'):
        status, customer, _ = self.request('POST','/v1/customers',{'email':'buyer@example.test'},
                                           account=account,key=key)
        self.assertEqual(status,200,customer)
        return customer['id']

    def intent(self, account, customer, key, **options):
        status, intent, _ = self.request('POST','/v1/payment_intents',
            {'amount':'1000','currency':'usd','customer':customer,**options},account=account,key=key)
        self.assertEqual(status,200,intent)
        return '/v1/payment_intents/' + intent['id']

    def saved_card(self, account, customer):
        initial = self.intent(account,customer,'initial',setup_future_usage='off_session')
        grant = self.grant(account,initial.rsplit('/',1)[1])
        status, result, _ = self.component(grant['control_capability'],account,'card_payment',
                                            'confirm',grant['provider_version'])
        self.assertEqual(status,200,result)
        return self.request('GET',initial,account=account)[1]['payment_method']

    def confirm(self, route, account, method, key, **options):
        return self.request('POST',route + '/confirm',{'payment_method':method,**options},
                            account=account,key=key)

    def test_saved_card_requires_scoped_component_confirmation_and_capture(self):
        owner, foreign = self.account(), self.account('foreign')
        self.ready()
        customer = self.customer(owner)
        initial = self.intent(owner,customer,'initial',setup_future_usage='off_session',capture_method='manual')
        capture_form = {'amount_to_capture':'1000'}
        self.assertEqual(self.confirm(initial,owner,'pm_card_visa','shortcut')[0],400)
        self.assertEqual(self.request('POST',initial + '/capture',capture_form,account=owner,key='uncaptured')[0],409)
        status, observed, _ = self.request('GET',initial,account=owner)
        self.assertEqual(status,200,observed)
        self.assertEqual(self.request('GET',initial,account=foreign)[0],403)
        self.assertEqual(self.exchange(observed['client_secret'],foreign,'card_payment')[0],403)
        self.assertEqual(self.admin('require_authentication_next')[0],200)
        status, grant, _ = self.exchange(observed['client_secret'],owner,'card_payment')
        self.assertEqual(status,200,grant)
        capability = grant['control_capability']
        self.assertEqual(self.component(capability,foreign,'card_payment','confirm',grant['provider_version'])[0],403)
        status, changed, _ = self.component(capability,owner,'card_payment','confirm',grant['provider_version'])
        self.assertEqual(status,200,changed)
        self.assertEqual(self.request('GET',initial,account=owner)[1]['status'],'requires_action')
        self.assertEqual(self.request('POST',initial + '/capture',capture_form,account=owner,key='before-auth')[0],409)
        self.assertEqual(self.store.objects(self.run,'payment_method'),[])
        self.assertEqual(self.component(capability,owner,'card_payment','complete_authentication',changed['provider_version'])[0],200)
        self.assertEqual(self.store.objects(self.run,'payment_method'),[])

        self.assertEqual(self.admin('drop_next_reply_after_commit')[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.request('POST',initial + '/capture',capture_form,account=owner,key='capture')
        self.restart()
        status, captured, _ = self.request('POST',initial + '/capture',capture_form,account=owner,key='capture')
        self.assertEqual(status,200,captured)
        self.assertEqual(captured['status'],'succeeded')
        saved = captured['payment_method']
        self.assertEqual(captured['latest_charge']['payment_method'],saved)
        self.assertEqual(self.request('GET',initial,account=owner)[1]['payment_method'],saved)
        method_path = '/v1/payment_methods/' + saved
        status, method, _ = self.request('GET',method_path,account=owner)
        self.assertEqual(status,200,method)
        self.assertEqual((method['customer'],method['type'],method['status']),(customer,'card','active'))
        self.assertEqual(method['initial_payment_intent'],captured['id'])
        self.assertEqual(self.request('GET',method_path,account=foreign)[0],403)
        self.assertEqual(len(self.store.objects(self.run,'payment_method')),1)
        self.assertEqual(len(self.store.objects(self.run,'charge')),1)

    def test_off_session_confirmation_is_scoped_and_recoverable_after_lost_reply(self):
        owner, foreign = self.account(), self.account('foreign')
        self.ready()
        customer = self.customer(owner)
        method = self.saved_card(owner,customer)
        renewal = self.intent(owner,customer,'renewal',application_fee_amount='100')
        other_customer = self.intent(owner,self.customer(owner,'second-customer'),'other-customer')
        other_account = self.intent(foreign,self.customer(foreign),'other-account')
        self.assertIsNone(self.request('GET',renewal,account=owner)[1]['client_secret'])
        self.assertEqual(self.confirm(renewal,owner,'pm_card_visa','direct')[0],400)
        self.assertEqual(self.confirm(renewal,owner,'pm_card_visa','universal',off_session='true')[0],400)
        self.assertEqual(self.confirm(renewal,owner,method,'missing-flag')[0],400)
        self.assertEqual(self.confirm(other_customer,owner,method,'wrong-buyer',off_session='true')[0],403)
        self.assertEqual(self.confirm(other_account,foreign,method,'wrong-account',off_session='true')[0],403)
        self.assertEqual(self.confirm(renewal,owner,new_id('pm'),'missing',off_session='true')[0],403)

        self.assertEqual(self.admin('drop_next_reply_after_commit')[0],200)
        with self.assertRaises((http.client.RemoteDisconnected,ConnectionResetError)):
            self.confirm(renewal,owner,method,'confirm',off_session='true')
        self.restart()
        status, captured, _ = self.confirm(renewal,owner,method,'confirm',off_session='true')
        self.assertEqual(status,200,captured)
        self.assertEqual((captured['status'],captured['customer'],captured['payment_method']),('succeeded',customer,method))
        self.assertEqual(captured['latest_charge']['application_fee_amount'],100)
        self.assertEqual(self.request('GET',renewal,account=owner)[1]['latest_charge'],captured['latest_charge'])
        self.assertEqual(self.confirm(renewal,owner,new_id('pm'),'confirm',off_session='true')[0],409)
        self.assertEqual(self.confirm(renewal,owner,method,'second-confirm',off_session='true')[0],409)
        self.assertEqual(self.request('GET','/v1/charges?payment_intent=' + captured['id'],account=owner)[1]['data'],
                         [captured['latest_charge']])
        self.assertEqual(len(self.store.objects(self.run,'payment_method')),1)
        events = [json.loads(row['payload']) for row in self.store.events(self.run)]
        self.assertEqual(sum(e['type'] == 'payment_intent.succeeded' and e['request']['idempotency_key'] == 'confirm'
                             for e in events),1)

    def test_unavailable_revoked_declined_and_interaction_required(self):
        account = self.account()
        self.ready()
        customer = self.customer(account)
        method = self.saved_card(account,customer)
        renewal = self.intent(account,customer,'renewal')

        def change(action):
            info = self.control('resources/inspect',{'kind':'payment_method','resource_id':method,'account':account})[1]
            request = {'action':action,'account':account,'expected_provider_version':info['provider_version']}
            endpoint = 'payment_methods/' + method + '/actions'
            self.assertEqual(self.control(endpoint,request,token=self.server.credentials.api_key)[0],403)
            status, result, _ = self.control(endpoint,request)
            self.assertEqual(status,200,result)
            self.assertEqual(self.control(endpoint,request)[0],409)

        change('make_unavailable')
        status, error, _ = self.confirm(renewal,account,method,'unavailable',off_session='true')
        self.assertEqual((status,error['error']['code']),(409,'simulator_payment_method_unavailable'))
        change('restore')
        for trigger, code, expected in [('decline_next_confirmation','card_declined','requires_payment_method'),
                                        ('require_authentication_next','authentication_required','requires_action')]:
            with self.subTest(trigger=trigger):
                self.assertEqual(self.admin(trigger)[0],200)
                status, error, _ = self.confirm(renewal,account,method,trigger,off_session='true')
                self.assertEqual(status,402,error)
                self.assertEqual(error['error']['code'],code)
                self.assertEqual(error['error']['payment_intent']['status'],expected)
                self.assertEqual(self.confirm(renewal,account,method,trigger,off_session='true')[:2],(status,error))
                observed = self.request('GET',renewal,account=account)[1]
                self.assertEqual((observed['status'],observed['last_payment_error']['code']),(expected,code))
                self.assertIsNone(observed['latest_charge'])
                self.assertIsNone(observed['client_secret'])
        change('revoke')
        another = self.intent(account,customer,'after-revocation')
        status, error, _ = self.confirm(another,account,method,'revoked',off_session='true')
        self.assertEqual((status,error['error']['code']),(409,'simulator_payment_method_revoked'))
        self.assertEqual(len(self.store.objects(self.run,'charge')),1)
