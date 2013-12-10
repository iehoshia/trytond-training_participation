#This file is part of Tryton.  The COPYRIGHT file at the top level of
#this repository contains the full copyright notices and license terms.
from decimal import Decimal
from datetime import datetime, timedelta, date
import operator
from itertools import izip, groupby
from sql import Column, Literal
from sql.aggregate import Sum
from sql.conditionals import Coalesce

from trytond.model import Workflow, ModelView, ModelSQL, fields
from trytond.wizard import Wizard, StateView, StateAction, StateTransition, \
    Button
from trytond.report import Report
from trytond.tools import reduce_ids
from trytond.pyson import Eval, PYSONEncoder, Date, Id
from trytond.transaction import Transaction
from trytond.pool import Pool
from trytond import backend

STATES = {
    'readonly': (Eval('state') != 'draft'),
}

STATES_CONFIRMED = {
    'readonly': (Eval('state') != 'draft'),
    'required': (Eval('state') == 'confirmed'),
}

GUARANTEE = [
    ('payment', 'Payment'),
    ('voucher', 'Voucher'),
    ('credit_card', 'Credit Card'),
    ('letter', 'Letter'),
    ]

class TrainingGroup(ModelView, ModelSQL):
    'Group'
    __name__ = 'training.group'
    
    name = fields.Char('Name', required=True, help="The group's name",)
    session = fields.Many2One('training.session', 'Session', required=True, ondelete='CASCADE')
    seances = fields.One2Many('training.seance', 'group', 'Seances', readonly=True)

    @classmethod
    def __setup__(cls):
        super(TrainingGroup, cls).__setup__()
        cls._sql_constraints += [
            ('uniq_name_session', 'UNIQUE(name, session_id)', 'It already exists a group with this name.'),
            ]

class TrainingSession(ModelView, ModelSQL):
    'Session'
    __name__ = 'training.session'

    def _has_shared_seances_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, False)
        for session in self.browse(cr, uid, ids, context=context):
            res[session.id] = any(seance.shared for seance in session.seance_ids)

        return res

    # training.session
    #def _name_compute(self, cr, uid, ids, name, args, context=None):
    #    res = dict.fromkeys(ids, '')

    #    for obj in self.browse(cr, uid, ids):
    #        date = time.strftime('%Y-%m-%d', time.strptime(obj.date, '%Y-%m-%d %H:%M:%S'))
    #        res[obj.id] = "[%s] %s (%s)" % (obj.kind[0].upper(),
    #                                        obj.offer_id.name,
    #                                        date,)

    #    return res

    # training.session
    def _store_get_participation(self, cr, uid, ids, context=None):
        result = set()

        for line in self.pool.get('training.subscription.line').browse(cr, 1, ids, context=context):
            result.add(line.session_id.id)

        return list(result)

    def _participant_count(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)

        cr.execute("""SELECT ss.session_id, COUNT(DISTINCT(tsl.contact_id))
                        FROM training_participation tp, training_subscription_line tsl, training_session_seance_rel ss
                       WHERE tp.subscription_line_id = tsl.id
                         AND ss.seance_id = tp.seance_id
                         AND ss.session_id in (%s)
                         AND tsl.state in ('confirmed', 'done')
                    GROUP BY ss.session_id
                   """ % (','.join(['%s'] * len(ids)),),
                   ids
                  )
        for seance_id, count in cr.fetchall():
            res[seance_id] = int(count)
        return res

    # training.session
    def _confirmed_subscriptions_count(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)
        sl_proxy = self.pool.get('training.subscription.line')
        for session_id in ids:
            res[session_id] = int(sl_proxy.search_count(cr, uid, [('session_id', '=', session_id),('state', 'in', ['confirmed', 'done'])], context=context))

        return res

    # training.session
    def _available_seats_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)

        for session in self.browse(cr, uid, ids, context=context):
            if session.manual:
                value_max = session.participant_count_manual
            else:
                value_max = session.participant_count
            res[session.id] = int(session.max_limit) - int(value_max)

        return res

    # training.session
    def _draft_subscriptions_count(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)

        proxy = self.pool.get("training.subscription.line")
        for session_id in ids:
            res[session_id] = int(proxy.search_count(cr, uid, [('session_id', '=', session_id),('state', '=', 'draft')], context=context))

        return res

    # training.session
    def _limit_all(self, cr, uid, ids, fieldnames, args, context=None):
        res = {}
        for obj in self.browse(cr, uid, ids, context=context):
            res[obj.id] = {'min_limit' : 0, 'max_limit' : 0}
            groups = {}
            def _add_to_group(seance_id, group_id):
                if seance_id not in groups:
                    groups[seance_id] = set()
                groups[seance_id].add(group_id)

            if len(obj.seance_ids) > 0:
                seances = iter(obj.seance_ids)
                seance = seances.next()
                if seance.group_id:
                    _c = seance.course_id and seance.course_id.id or 0
                    _add_to_group(_c, seance.group_id.id)
                value_min = seance.min_limit
                value_max = seance.max_limit

                for seance in seances:
                    if seance.group_id:
                        _c = seance.course_id and seance.course_id.id or 0
                        _add_to_group(_c, seance.group_id.id)
                    value_min = min(seance.min_limit, value_min)
                    value_max = min(seance.max_limit, value_max)

                max_groups = 0
                for v in groups.values():
                    if len(v) > max_groups:
                        max_groups = len(v)
                res[obj.id]['min_limit'] = value_min
                res[obj.id]['max_limit'] = value_max * max(max_groups, 1)

        return res

    def _min_limit_reached(self, cr, uid, ids, fn, args, context=None):
        result = dict.fromkeys(ids, False)
        for session in self.browse(cr, uid, ids, context):
            count = ['participant_count', 'participant_count_manual'][session.manual]
            result[session.id] =  session[count] >= session.min_limit
        return result

    # training.session
    def _store_get_seances(self, cr, uid, ids, context=None):
        values = set()

        for obj in self.pool.get('training.seance').browse(cr, uid, ids, context=context):
            for session in obj.session_ids:
                values.add(session.id)

        return list(values)

    name = fields.Char('Name', required=True)
    state = fields.Selection([('draft', 'Draft'),
                                    ('opened', 'Opened'),
                                    ('opened_confirmed', 'Confirmed'),
                                    ('closed_confirmed', 'Closed Subscriptions'),
                                    ('inprogress', 'In Progress'),
                                    ('closed', 'Closed'),
                                    ('cancelled', 'Cancelled')],
                                   'State',
                                   required=True,
                                   readonly=True,
                                   help="The status of the session",
                                  )
    groups = fields.One2Many('training.group', 'session', 'Group', readonly=True)
    done = fields.boolean('Done')
    offer = fields.Many2One('training.offer',
                                     'Offer',
                                     required=True,
                                     help="Allows to select a validated offer for the session",
                                     domain=[('state', '=', 'validated')]
                                    )
    
    seances = fields.Many2Many('training.seance',
                                        'training_session_seance_rel',
                                        'session_id',
                                        'seance_id',
                                        'Seances',
                                        ondelete='CASCADE',
                                        help='List of the events in the session')
    date = fields.DateTime('Date',
                                 required=True,
                                 help="The date of the planned session"
                                )
    date_end = fields.DateTime('End Date',
                                 help="The end date of the planned session"
                                )

    faculty = fields.Many2One('party.party',
                                    'Responsible',
                                    required=True, 
                                    domain = [('is_faculty','=',True)])
    
    ''' participant_count = fields.function(_participant_count,
                                              method=True,
                                              string='Total Confirmed Seats',
                                              type='integer',
                                             )''' 

    '''
    confirmed_subscriptions = fields.function(_confirmed_subscriptions_count,
                                                    method=True,
                                                    string='Confirmed Subscriptions',
                                                    type='integer',
                                                   ),''' 
    '''draft_subscriptions' : fields.function(_draft_subscriptions_count,
                                                method=True,
                                                string="Draft Subscriptions",
                                                type="integer",
                                                help="Draft Subscriptions for this session",
                                               ),'''

    '''subscription_line_ids': fields.one2many('training.subscription.line',
                                                 'session_id',
                                                 'Subscription Lines',
                                                 readonly=True),'''

    manual = fields.Boolean('Manual', help="Allows to the user to specify the number of participants"),
    min_limit = fields.Integer('Mininum Threshold',
                               help="The minimum threshold is the minimum of the minimum threshold of each seance",
                                )
    max_limit = fields.Integer('Maximum Threshold',
                                      help="The maximum threshold is the minimum of the maximum threshold of each seance"
                                      )

    request_ids = fields.one2many('training.participation.stakeholder.request', 'session', 'Requests')
    
    def _check_date_before_now(self, cr, uid, ids, context=None):
        obj = self.browse(cr, uid, ids[0], context=context)
        res = obj.date >= datetime.strftime('%Y-%m-%d %H:%M:%S')
        return res

    def _check_date_of_seances(self, cr, uid, ids, context=None):
        for session in self.browse(cr, uid, ids, context=context):
            for seance in session.seance_ids:
                if seance.date < session.date:
                    return False

        return True

    _constraints = [
        #(_check_date_before_now, "You cannot create a date before now", ['date']),
        #(_check_date_holiday, "You cannot assign a date in a public holiday", ['date']),

        (_check_date_of_seances, "You have a seance with a date inferior to the session's date", ['date']),
    ]

    def default_state(self):
        return 'draft'
    
    def default_manual(self):
        return 0
    
    def default_min_limit(self):
        return 1
    
    def default_max_limit(self):
        return 1
    
    '''
    def _create_seance(self, cr, uid, session, context=None):
        seance_ids = []
        seance_proxy = self.pool.get('training.seance')

        def get_list_of_courses(lst, course):
            if course.course_ids:
                for child in course.course_ids:
                    get_list_of_courses(lst, child)
            else:
                lst.append(course)

        group_proxy = self.pool.get('training.group')
        group_ids = group_proxy.search(cr, uid, [('session_id', '=', session.id)])
        if group_ids:
            group_id = group_ids[0]
        else:
            group_id = group_proxy.create(cr, uid, {'name' : _('Class %d') % (1,), 'session_id': session.id}, context=context)

        def _inner_create_seance(item, session, date, duration, master_seance_id = None):
            values = {
                'name' : self.pool.get('training.seance').on_change_course(cr, uid, [], item.id, session.kind, context=context)['value']['name'],
                'original_session_id' : session.id,
                'course_id' : item.id,
                'kind': item.kind,
                'min_limit' : item.course_type_id.min_limit,
                'max_limit' : item.course_type_id.max_limit,
                'user_id' : session.user_id.id,
                'date' : date.strftime('%Y-%m-%d %H:%M:%S'),
                'master_id' : master_seance_id,
                'duration' : duration,
                'group_id': group_id,
            }
            if session.manual:
                values['manual'] = session.manual
                values['participant_count_manual'] = session.participant_count_manual

            return seance_proxy.create(cr, uid, values, context=context)



        planned_seance_ids = []
        planned_course_ids = set()
        if session.seance_ids:
            proxy_seance = self.pool.get('training.seance')

            for seance in session.seance_ids:
                planned_course_ids.add(seance.course_id.id)
                if seance.master_id:
                    planned_seance_ids.extend(proxy_seance.search(cr, uid, [('master_id', '=', seance.master_id.id)], context=context))
                else:
                    planned_seance_ids.append(seance.id)
                    planned_seance_ids.extend(proxy_seance.search(cr, uid, [('master_id', '=', seance.id)], context=context))

        seance_counter = 0

        lst = []
        for course in session.offer_id.course_ids:
            if course.course_id.id in planned_course_ids:
                continue
            tmp_lst = []
            get_list_of_courses(tmp_lst, course.course_id)

            splitted_by = int(course.course_id.splitted_by) or 8
            for item in tmp_lst:
                duration = item.duration
                while duration > 0:
                    seance_counter += 1
                    duration -= splitted_by

            lst.extend(tmp_lst)

        dates = []

        # Day by day search for a valid working day
        rec = 0
        max_rec = 365
        while len(dates) < seance_counter and rec < max_rec:
            try_session_date = sdate.strftime('%Y-%m-%d')
            cr.execute(
                "SELECT count(*) "
                "FROM training_holiday_period p "
                "WHERE date(%s) >= p.date_start "
                "  AND date(%s) <= p.date_stop ",
                (try_session_date, try_session_date))
            r = cr.fetchall()
            if len(r) == 1 and r[0][0] == 0:
                dates.append(sdate)
            sdate += sdate_incr
            rec += 1

        if not dates:
            cr.execute(
                "SELECT date(%s) + s.t AS date FROM generate_series(0,%s) AS s(t)",
                (session.date, seance_counter+1))

            for x in cr.fetchall():
                dates.append(mx.DateTime.strptime(x[0] + " " + date.strftime('%H:%M:%S'), '%Y-%m-%d %H:%M:%S'))

        # later we will use date.pop() so we need to reverse date,
        # so that first date are a end of array, at poped first
        dates.reverse()

        def create_purchase_lines(purchase_line_ids, seance_id, procurement_quantity):
            proxy = self.pool.get('training.seance.purchase_line')
            for pl in purchase_line_ids:
                if pl.procurement_quantity == procurement_quantity:
                    if pl.attachment_id:
                        product_price = pl.attachment_price
                        description = "%s (%s)" % (pl.product_id.name, pl.attachment_id.datas_fname)
                    else:
                        product_price = pl.product_price
                        description = pl.description or pl.product_id.name

                    if pl.description:
                        description = "%s - %s" % (description, pl.description,)

                    values = {
                        'seance_id' : seance_id,
                        'course_id': pl.course_id.id,
                        'product_id' : pl.product_id.id,
                        'description' : description,
                        'product_qty' : pl.product_qty,
                        'product_uom' : pl.product_uom.id,
                        'product_price' : product_price,
                        'fix' : pl.fix,
                        'attachment_id' : pl.attachment_id and pl.attachment_id.id,
                    }

                    purchase_line_id = proxy.create(cr, uid, values, context=context)

        seance_id = None
        first_seance_id = None
        for item in lst:
            duration = item.duration
            splitted_by = int(item.splitted_by) or 8

            master_seance_id = None
            counter_part = 0
            while duration > 0:
                date = dates.pop()
                tmp_id = _inner_create_seance(item, session, date, duration <= splitted_by and duration or splitted_by, master_seance_id)

                proxy = self.pool.get('training.seance.purchase_line')
                for pl in session.offer_id.purchase_line_ids:
                    if pl.procurement_quantity == 'on_all_seances':
                        if pl.attachment_id:
                            product_price = pl.attachment_price
                            description = "%s (%s)" % (pl.product_id.name, pl.attachment_id.datas_fname)
                        else:
                            product_price = pl.product_price
                            description = pl.product_id.name

                        if pl.description:
                            description = "%s - %s" % (description, pl.description,)

                        values = {
                            'seance_id' : tmp_id,
                            'course_id': pl.course_id.id,
                            'product_id' : pl.product_id.id,
                            'description' : description,
                            'product_qty' : pl.product_qty,
                            'product_uom' : pl.product_uom.id,
                            'product_price' : product_price,
                            'fix' : pl.fix,
                            'attachment_id' : pl.attachment_id and pl.attachment_id.id,
                        }

                        purchase_line_id = proxy.create(cr, uid, values, context=context)

                if master_seance_id is None:
                    master_seance_id = tmp_id

                seance_ids.append(tmp_id)

                duration -= splitted_by

            seance_id = master_seance_id

            if master_seance_id:
                seance = self.pool.get('training.seance').browse(cr, uid, master_seance_id, context=context)

                proxy = self.pool.get('training.seance.purchase_line')
                for pl in session.offer_id.purchase_line_ids:
                    if pl.procurement_quantity == 'on_seance_course' and pl.course_id and pl.course_id.id == seance.course_id.id:
                        if pl.attachment_id:
                            product_price = pl.attachment_price
                            description = "%s (%s)" % (pl.product_id.name, pl.attachment_id.datas_fname)
                        else:
                            product_price = pl.product_price
                            description = pl.product_id.name

                        if pl.description:
                            description = "%s - %s" % (description, pl.description,)

                        values = {
                            'seance_id' : master_seance_id,
                            'course_id': pl.course_id.id,
                            'product_id' : pl.product_id.id,
                            'description' : description,
                            'product_qty' : pl.product_qty,
                            'product_uom' : pl.product_uom.id,
                            'product_price' : product_price,
                            'fix' : pl.fix,
                            'attachment_id' : pl.attachment_id and pl.attachment_id.id,
                        }

                        purchase_line_id = proxy.create(cr, uid, values, context=context)

            if first_seance_id is None:
                first_seance_id = seance_id

        if first_seance_id:
            self.pool.get('training.seance').write(cr, uid, [first_seance_id], {'is_first_seance' : 1}, context=context)
            create_purchase_lines(session.offer_id.purchase_line_ids, first_seance_id, 'on_first_seance')

        if seance_id != first_seance_id:
            create_purchase_lines(session.offer_id.purchase_line_ids, seance_id, 'on_last_seance')

        return list(set(seance_ids + planned_seance_ids))'''

    # training.session
    def action_create_seances(self, cr, uid, ids, context=None):
        for session in self.browse(cr, uid, ids, context=context):
            seance_ids = self._create_seance(cr, uid, session, context)

            self.write(cr, uid, session.id, {'seance_ids' : [(6, 0, seance_ids)]}, context=context)

        return True

    def on_change_offer(self, cr, uid, ids, offer_id, context=None):
        if not offer_id:
            return False

        offer_proxy = self.pool.get('training.offer')
        offer = offer_proxy.browse(cr, uid, offer_id, context=context)

        return {
            'value' : {
                'kind' : offer.kind,
                'name' : offer.name
            }
        }

    def on_change_date(self, cr, uid, ids, date, offer_id, context=None):
        old_date = ids and self.browse(cr, uid, ids[0], context=context).date or 0

        if self.pool.get('training.holiday.period').is_in_period(cr, date):
            return {
                'value' : {
                    'date' : old_date,
                },
                'warning' : {
                    'title' : _("Selection Date"),
                    'message' : _("You can not select this date because it is a public holiday"),
                },
            }
        return {}


    # training.session
    def _create_participation(self, cr, uid, ids, subscription_line, context=None):
        proxy = self.pool.get('training.participation')
        proxy_seance = self.pool.get('training.seance')

        if subscription_line.session_id.group_ids:
            for group in subscription_line.session_id.group_ids:
                if len(group.seance_ids) > 0:
                    for seance in group.seance_ids:
                        participation_id = proxy_seance._create_participation(cr, uid, seance, subscription_line, context=context)
                        if seance.state == 'confirmed':
                            proxy.create_procurements(cr, uid, [participation_id], delayed=True, context=context)
                    break
        else:
            for seance in subscription_line.session_id.seance_ids:
                participation_id = proxy_seance._create_participation(cr, uid, seance, subscription_line, context=context)
                if seance.state == 'confirmed':
                    proxy.create_procurements(cr, uid, [participation_id], delayed=True, context=context)

    # training.session
    def action_workflow_draft(self, cr, uid, ids, context=None):
        return self.write(cr, uid, ids, {'state' : 'draft'}, context=context)

    # training.session
    def test_workflow_open(self, cr, uid, ids, context=None):
        for obj in self.browse(cr, uid, ids, context=context):
            if not len(obj.seance_ids):
                raise osv.except_osv(_('Warning'), _("Please, do not forget to have the seances in your session"))
            else:
                min_date = obj.date
                for seance in obj.seance_ids:
                    if seance.state == 'draft':
                        raise osv.except_osv(_('Warning'), _('Please, you have at least a draft seance'))
                    else:
                        if seance.date < obj.date:
                            raise osv.except_osv(_('Warning'), _("Please, Check the date of your seances because there is one seance with a date inferior to the session's date"))

                    min_date = min(min_date, seance.date)

        return True

    # training.session
    def action_workflow_open(self, cr, uid, ids, context=None):
        return self.write(cr, uid, ids, {'state' : 'opened'}, context=context)

    # training.session
    def action_workflow_open_confirm(self, cr, uid, ids, context=None):

        proxy = self.pool.get('training.subscription.line')
        subscription_line_ids = proxy.search(cr, uid, [('session_id', 'in', ids), ('state', '=', 'confirmed')], context=context)
        proxy.send_email(cr, uid, subscription_line_ids, 'session_open_confirmed', context)

        proxy = self.pool.get('training.participation.stakeholder')
        for session in self.browse(cr, uid, ids, context=context):
            objs = {}
            for seance in session.seance_ids:
                for contact in seance.contact_ids:
                    if contact.state == 'accepted':
                        objs.setdefault(contact.id, {}).setdefault('seances', []).append(seance)

            proxy.send_email(cr, uid, objs.keys(), 'session_open_confirmed', session, context, objs)

        return self.write(cr, uid, ids, {'state' : 'opened_confirmed'}, context=context)

    # training.session
    def test_workflow_open_confirm(self, cr, uid, ids, context=None):
        return True

        # Disabled code

        for obj in self.browse(cr, uid, ids, context=context):
            # Check the minimum for this session
            number_of_participants = proxy.search_count(cr, uid, [('session_id', '=', obj.id)], context=context)
            if number_of_participants:
                if number_of_participants <= obj.min_limit:
                    raise osv.except_osv(_('Warning'),
                                         _('The number of participants is less than the required minimal limit'))

            # Check the date of the first seance > J-X
            #date = mx.DateTime.strptime(obj.seance_ids[0].date, '%Y-%m-%d %H:%M:%S')
            #new_date = (mx.DateTime.now() + mx.DateTime.RelativeDate(days=number_of_days)).strftime('%Y-%m-%d %H:%M:%S')
            #if mx.DateTime.now() > date -mx.DateTime.strptime(new_date, '%Y-%m-%d %H:%M:%S') :
            #    raise osv.except_osv(_('Warning'),
            #                         _('There is a seance with a start date inferior to %(days)s day(s)') % values)

        return True

    # training.session
    def action_workflow_close_confirm(self, cr, uid, ids, context=None):
        #proxy = self.pool.get('training.participation.stakeholder')
        #for session in self.browse(cr, uid, ids, context):
        #    objs = {}
        #    for seance in session.seance_ids:
        #        for contact in seance.contact_ids:
        #            if contact.state == 'confirmed':
        #                objs.setdefault(contact.id, {}).setdefault('seances', []).append(seance)

        #    proxy.send_email(cr, uid, objs.keys(), '???', session, context, objs)

        return self.write(cr, uid, ids, {'state' : 'closed_confirmed'}, context=context)

    # training.session
    def action_create_invoice(self, cr, uid, ids, context=None):
        sl_proxy = self.pool.get('training.subscription.line')
        for session in self.browse(cr, uid, ids, context=context):
            sl_ids = sl_proxy.search(cr, uid, [('session_id', '=', session.id),('invoice_line_id', '=', False),('state', 'in', ('confirmed', 'done'))], context=context)
            sl_proxy.action_create_invoice(cr, uid, sl_ids, context=context)

        return True

    # training.session
    def action_workflow_inprogress(self, cr, uid, ids, context=None):
        self.action_create_invoice(cr, uid, ids, context=context)
        return self.write(cr, uid, ids, {'state' : 'inprogress'}, context=context)

    # training.session
    def action_workflow_close(self, cr, uid, ids, context=None):
        workflow = netsvc.LocalService('workflow')
        proxy = self.pool.get('training.subscription.line')
        for session in self.browse(cr, uid, ids, context):
            subscription_line_ids = proxy.search(cr, uid, [('session_id', '=', session.id), ('state', '=', 'confirmed')], context=context)
            for sl_id in subscription_line_ids:
                workflow.trg_validate(uid, 'training.subscription.line', sl_id, 'signal_done', cr)

        return self.write(cr, uid, ids, {'state' : 'closed'}, context=context)

    # trainin.session
    def test_workflow_close(self, cr, uid, ids, context=None):
        return all(seance.state in ('done','cancelled') for session in self.browse(cr, uid, ids, context=context)
                                          for seance in session.seance_ids)

    # training.session
    def action_cancellation_session(self, cr, uid, ids, context=None):

        # just send emails...

        proxy = self.pool.get('training.subscription.line')
        subscription_line_ids = proxy.search(cr, uid, [('session_id', 'in', ids), ('state', '=', 'confirmed')], context=context)
        proxy.send_email(cr, uid, subscription_line_ids, 'session_confirm_cancelled', context)

        proxy = self.pool.get('training.participation.stakeholder')
        for session in self.browse(cr, uid, ids, context=context):
            objs = {}
            for seance in session.seance_ids:
                for contact in seance.contact_ids:
                    if contact.state == 'accepted':
                        objs.setdefault(contact.id, {}).setdefault('seances', []).append(seance)

            proxy.send_email(cr, uid, objs.keys(), 'session_confirm_cancelled', session, context, objs)

    # training.session
    def action_workflow_cancel(self, cr, uid, ids, context=None):
        self.write(cr, uid, ids, {'state' : 'cancelled'}, context=context)

        workflow = netsvc.LocalService('workflow')
        for session in self.browse(cr, uid, ids, context=context):

            if not session.has_shared_seances:
                for request in session.request_ids:
                    workflow.trg_validate(uid, 'training.participation.stakeholder.request', request.id, 'signal_cancel', cr)
            else:
                ### What to do with requests to shared seances ?
                pass

            for seance in session.seance_ids:
                workflow.trg_validate(uid, 'training.seance', seance.id, 'signal_cancel', cr)

            for subline in session.subscription_line_ids:
                workflow.trg_validate(uid, 'training.subscription.line', subline.id, 'signal_cancel', cr)

        return True


    
    def search(self, cr, uid, domain, offset=0, limit=None, order=None, context=None, count=False):
        subscription_id = context and context.get('subscription_id', False) or False

        if subscription_id:
            proxy = self.pool.get('training.subscription.line')
            ids = proxy.search(cr, uid, [('subscription_id', '=', subscription_id)], context=context)
            return [sl.session_id.id for sl in proxy.browse(cr, uid, ids, context=context)]


        return super(training_session, self).search(cr, uid, domain, offset=offset, limit=limit, order=order, context=context, count=count)

    def copy(self, cr, uid, object_id, values, context=None):
        raise osv.except_osv(_("Error"),
                             _("You can not duplicate a session"))

class TrainingParticipation(ModelView, ModelSQL):
    'Participation'
    _name = 'training.participation'
    
    def _store_get_sublines(self, cr, uid, sl_ids, context=None):
        sublines = self.pool.get('training.subscription.line')
        result = []
        for subline in sublines.browse(cr, uid, sl_ids, context):
            result.extend(p.id for p in subline.participation_ids)
        return result

    seance = fields.Many2One('training.seance', 'Seance', 
                             required=True, readonly=True, ondelete='CASCADE'),
    subscription = fields.Many2One('training.subscription', 'Subscription', 
                                           required=True, readonly=True, ondelete='CASCADE')
    
    present = fields.Boolean('Present', 
                             help="Allows to know if a participant was present or not")

    summary = fields.Text('Summary')

    def default_present(self):
        return 0
        
    _sql_constraints = [
        ('uniq_seance_sl', 'unique(seance_id, subscription_line_id)', "The subscription and the seance must be unique !"),
    ]

    def on_change_seance(self, cr, uid, ids, seance_id, context=None):
        if not seance_id:
            return {'value' : {'group_id' : 0}}

        seance = self.pool.get('training.seance').browse(cr, uid, seance_id, context=context)

        return {
            'value' : {
                'group_id' : seance.group_id and seance.group_id.id,
                'date' : seance.date,
            }
        }


    def name_get(self, cr, uid, ids, context=None):
        res = []
        for obj in self.browse(cr, uid, list(set(ids)), context=context):
            sl = obj.subscription_line_id
            oid = obj.id
            if sl.contact_id:
                name = "%s %s (%s)" % (sl.job_id.contact_id.first_name, sl.job_id.contact_id.name, sl.partner_id.name,)
            else:
                name = super(training_participation, self).name_get(cr, uid, [oid], context=context)[0][1]
            res.append((oid, name,))
        return res

    # training.participation
    def create_procurements(self, cr, uid, participation_ids, delayed=False, context=None):
        purchase_order_pool = self.pool.get('purchase.order')
        products = {}
        for participation in self.browse(cr, uid, participation_ids, context=context):
            if participation.seance_id and participation.seance_id.purchase_line_ids:
                for purchase_line in participation.seance_id.purchase_line_ids:
                    products.setdefault(purchase_line, [0.0, []])
                    products[purchase_line][0] = purchase_line.product_qty

                    if purchase_line.fix == 'by_subscription':
                        products[purchase_line][0] = purchase_line.product_qty * len(participation_ids)

                    products[purchase_line][1].append(participation)

        location_id = self.pool.get('stock.location').search(cr, uid, [('usage', '=', 'internal')], context=context)[0]

        participations = {}
        for po_line, (quantity, parts) in products.items():
            # Create purchase order from this po_line ('seance.purchase.line')
            purchase_id = purchase_order_pool.create_from_procurement_line(cr, uid, po_line, quantity, location_id, context=context)
            purchase = self.pool.get('purchase.order').browse(cr, uid, purchase_id, context=context)
            # Then get ids of all create purchase.order.line
            purchase_order_line_ids = [ pol.id for pol in purchase.order_line ]

            for part in parts:
                participations.setdefault(part, []).extend(purchase_order_line_ids)

        # write relate purchase.order.line on each participations
        for participation, purchase_ids in participations.items():
            participation.write({'purchase_ids' : [(6, 0, purchase_ids)]}, context=context)

        # mark the purchase as done for this participations
        return self.write(cr, uid, participation_ids, {'purchase_state' : 'done'}, context=context)

    def unlink(self, cr, uid, ids, context=None):
        # TODO cancel the procurements ??
        return super(TrainingParticipation, self).unlink(cr, uid, ids, context=context)

class TrainingSeanse(ModelView, ModelSQL):
    'Seance'
    _name = 'training.seance'

    def _shared_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)
        for seance in self.browse(cr, uid, ids, context=context):
            res[seance.id] = len(seance.session_ids) > 1
        return res

    # training.seance
    def _available_seats_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)

        for seance in self.browse(cr, uid, ids, context=context):
            count = ['participant_count', 'participant_count_manual'][seance.manual]
            res[seance.id] = seance.max_limit - int(seance[count])

        return res

    # training.seance
    def _draft_seats_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 0)

        cr.execute("SELECT rel.seance_id, count(1) "
                   "FROM training_subscription_line sl, training_session_seance_rel rel "
                   "WHERE sl.state = 'draft' "
                   "AND sl.session_id = rel.session_id "
                   "AND rel.seance_id IN (" + ",".join(['%s'] * len(ids)) + ") "
                   "GROUP BY rel.seance_id ", ids)

        for seance_id, count in cr.fetchall():
            res[seance_id] = int(count)

        return res

    # training.seance
    def _participant_count(self, cr, uid, ids, name, args, context=None):
        res = dict.fromkeys(ids, 0)

        cr.execute('SELECT tp.seance_id, COUNT(DISTINCT(tsl.contact_id)) '
                   'FROM training_participation tp, training_subscription_line tsl '
                   'WHERE tp.subscription_line_id = tsl.id '
                   'AND tp.seance_id in (' + ",".join(['%s'] * len(ids)) + ") "
                   "AND tsl.state in ('confirmed', 'done') "
                   'GROUP BY tp.seance_id',
                   ids
                  )
        for seance_id, count in cr.fetchall():
            res[seance_id] = int(count)

        return res

    _order = "date asc"

    def _confirmed_lecturer_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 'no')
        proxy = self.pool.get('training.participation.stakeholder')
        for seance in self.browse(cr, uid, ids, context=context):
            #if seance.kind == 'standard':
                has = len(seance.contact_ids) > 0 and any(c.state in ['accepted', 'done'] for c in seance.contact_ids)
                res[seance.id] = ['no', 'yes'][has]

        return res

    def _get_stakeholders(self, cr, uid, ids, context=None):
        values = set()
        for part in self.pool.get('training.participation.stakeholder').browse(cr, uid, ids, context=context):
            values.add(part.seance_id.id)

        return list(values)

    def _get_sessions_type(self, cr, uid, ids, fieldnames, args, context=None):
        res = []
        for seance in self.browse(cr, uid, ids, context=context):
            types = set()
            for session in seance.session_ids:
                if session.offer_id:
                    types.add(session.offer_id.kind.capitalize())
            res.append((seance.id, types))
        res = dict((x[0], ' / '.join(map(_, x[1]))) for x in res)
        return res

    def _contact_names_compute(self, cr, uid, ids, fieldnames, args, context=None):
        res = dict.fromkeys(ids, 'None')
        for seance in self.browse(cr, uid, ids, context=context):
            name = []
            for contact in seance.contact_ids:
                # skip lecturer request which has been cancelled
                if contact.state in ['cancelled','refused']:
                    continue
                if contact.job_id:
                    lecturer_name = "%s %s" % (contact.job_id.contact_id.name, contact.job_id.contact_id.first_name,)
                    if contact.state == 'draft':
                        name.append("[%s]" % (lecturer_name))
                    else:
                        name.append(lecturer_name)

                else:
                    tools.debug("Check this job: %r" % (contact.job_id))
            res[seance.id] = ", ".join(name)
        return res

    # training.seance
    def name_get(self, cr, uid, ids, context=None):
        return [(obj.id, "%s (%s)" % (obj.name, obj.group_id.name or _('Class %d') % (1,))) for obj in self.browse(cr, uid, list(set(ids)), context=context)]

    def on_change_course(self, cr, uid, ids, course_id, kind, context=None):
        if not course_id:
            return {
                'value' : {
                    'min_limit' : 0,
                    'max_limit' : 0,
                    'duration' : 0.0,
                }
            }

        course = self.pool.get('training.course').browse(cr, uid, course_id, context=context)

        return {
            'value':{
                'name' : course.name,
                'min_limit' : course.course_type_id.min_limit,
                'max_limit' : course.course_type_id.max_limit,
                'duration' : course.duration,
            }
        }


    _columns = {
        'id' : fields.integer('Database ID', readonly=True),
        'is_first_seance' : fields.boolean('First Seance', select=1),
        'name' : fields.char('Name', size=64, required=True, select=1),
        'session_ids' : fields.many2many('training.session',
                                         'training_session_seance_rel',
                                         'seance_id',
                                         'session_id',
                                         'Sessions',
                                         select=1,
                                         ondelete='cascade'),
        'sessions_type': fields.function(_get_sessions_type,
                                         method=True,
                                         string='Session(s) Type',
                                         type='char',
                                         size=32,
                                         ),
        'forced_lecturer' : fields.boolean('Forced Lecturer(s)'),
        'confirmed_lecturer' : fields.function(_confirmed_lecturer_compute,
                                               method=True,
                                               select=1,
                                               store={
                                                   'training.participation.stakeholder' : (_get_stakeholders, None, 10),
                                               },
                                               string="Confirmed Lecturer",
                                               type='selection',
                                               selection=[('no', 'No'),('yes','Yes')],
                                              ),
        'original_session_id' : fields.many2one('training.session', 'Original Session', ondelete='cascade'),
        'duplicata' : fields.boolean('Duplicata', required=True),
        'duplicated' : fields.boolean('Duplicated', required=True),
        'date' : fields.datetime('Date', required=True, select=1, help="The create date of seance"),
        'duration' : fields.float('Duration', select=1, help="The duration of the seance"),
        'participant_ids' : fields.one2many('training.participation',
                                            'seance_id',
                                            'Participants',
#                                            domain="[('group_id', '=', group_id)]" #error in v6.0 RC1
                                            ),
        'group_id' : fields.many2one('training.group', 'Group',
                                     #required=True,
                                     help='The group of participants',
                                    ),
        'state' : fields.selection([('opened', 'Opened'),
                                    ('confirmed', 'Confirmed'),
                                    ('inprogress', 'In Progress'),
                                    ('closed', 'Closed'),
                                    ('cancelled', 'Cancelled'),
                                    ('done', 'Done')],
                                   'State',
                                   required=True,
                                   readonly=True,
                                   select=1,
                                   help="The status of the Seance",
                                  ),
        'contact_ids' : fields.one2many('training.participation.stakeholder', 'seance_id', 'Lecturers', readonly=True),
        'contact_names' : fields.function(_contact_names_compute, method=True,
                                          type='char', size=256,
                                          string='Lecturers'),
        'course_id' : fields.many2one('training.course',
                                      'Course',
                                      select=1,
                                      domain="[('state_course', '=', 'validated')]"),
        'state_course' : fields.related('course_id', 'state_course',
                                        string="Course's State",
                                        type='selection',
                                        selection=[('draft', 'Draft'),
                                                   ('pending', 'Pending'),
                                                   ('deprecated', 'Deprecated'),
                                                   ('validated', 'Validated')],
                                        readonly=True),
        'purchase_line_ids' : fields.one2many('training.seance.purchase_line', 'seance_id', 'Supplier Commands'),
        'min_limit' : fields.integer("Minimum Threshold", help='The Minimum of Participants in Seance'),
        'max_limit' : fields.integer("Maximum Threshold", help='The Maximum of Participants in Seance'),
        'user_id' : fields.many2one('res.users', 'Responsible', required=True, select=1),

        'available_seats' : fields.function(_available_seats_compute,
                                            method=True,
                                            string='Available Seats',
                                            type='integer',
                                            help='Available seats in Seance'
                                           ),
        'draft_seats' : fields.function(_draft_seats_compute,
                                        method=True,
                                        string='Draft Subscriptions',
                                        type='integer',
                                        help='Draft Subscriptions',
                                       ),

        'presence_form' : fields.selection([('yes', 'Yes'),
                                            ('no', 'No')],
                                           'Presence Form Received',
                                           help='The training center has received the presence list from the lecturer'),
        'shared' : fields.function(_shared_compute,
                                   method=True,
                                   string='Shared',
                                   type='boolean',
                                   help="Allows to know if the seance is linked with a lot of sessions"),

        'kind': fields.selection(training_course_kind_compute, 'Kind', required=True, select=1),
        'master_id' : fields.many2one('training.seance', 'Master Seance'),

        'participant_count' : fields.function(_participant_count,
                                              method=True,
                                              type="integer",
                                              string="Confirmed Seats",
                                              help="Confirmed Subscriptions for this seance",
                                             ),
        'participant_count_manual' : fields.integer('Manual Confirmed Seats',
                                                    help="The quantity of supports, catering, ... relative to the number of participants coming from the confirmed seats"),
        'manual' : fields.boolean('Manual', help="Allows to the user to specify the number of participants"),
    }

    def _check_limits(self, cr, uid, ids, context=None):
        obj = self.browse(cr, uid, ids)[0]
        return obj.min_limit <= obj.max_limit

    def _check_date_before_now(self,cr,uid,ids,context=None):
        obj = self.browse(cr, uid, ids[0])
        res = obj.date > time.strftime('%Y-%m-%d %H:%M:%S')
        return res

    def _check_date_holiday(self, cr, uid, ids, context=None):
        obj = self.browse(cr, uid, ids[0], context=context)
        date = time.strftime('%Y-%m-%d', time.strptime(obj.date, '%Y-%m-%d %H:%M:%S'))
        return not self.pool.get('training.holiday.period').is_in_period(cr, date)

    def _check_date_of_sessions(self, cr, uid, ids, context=None):
        for seance in self.browse(cr, uid, ids, context=context):
            for session in seance.session_ids:
                if seance.date < session.date:
                    return False

        return True

    _constraints = [
        #(_check_date_before_now, "You cannot create a date before now", ['date']),
        #(_check_date_holiday, "You cannot assign a date in a public holiday", ['date']),
        (_check_limits, 'The minimum limit is greater than the maximum limit', ['min_limit', 'max_limit']),
        (_check_date_of_sessions, "You have a session with a date inferior to the seance's date", ['date']),
    ]

    _defaults = {
        'min_limit' : lambda *a: 0,
        'max_limit' : lambda *a: 0,
        'user_id' : lambda obj,cr,uid,context: uid,
        'presence_form' : lambda *a: 'no',
        'confirmed_lecturer' : lambda *a: 'no',
        'state' : lambda *a: 'opened',
        'date' : lambda *a: time.strftime('%Y-%m-%d %H:%M:%S'),
        'kind' : lambda *a: 'standard',
        'duplicata' : lambda *a: 0,
        'duplicated' : lambda *a: 0,
        'is_first_seance' : lambda *a: 0,
        'duration' : lambda *a: 2.0,
        'forced_lecturer' : lambda *a: 0,
    }

    # training.seance
    def action_workflow_open(self, cr, uid, ids, context=None):
        return self.write(cr, uid, ids, {'state' : 'opened'}, context=context)

    # training.seance
    def test_workflow_confirm(self, cr, uid, ids, context=None):
        for seance in self.browse(cr, uid, ids, context=context):
            if any(session.state in ('draft', 'opened') for session in seance.session_ids):
                raise osv.except_osv(_('Warning'),
                                     _('There is at least a session in the "Draft" or "Confirmed" state'))

        return True

    # training.seance
    def action_workflow_confirm(self, cr, uid, ids, context=None):
        proxy = self.pool.get('training.participation')
        emails = self.pool.get('training.email')
        report = netsvc.LocalService('report.training.seance.support.delivery.report')

        if not context:
            context = {}
        report_ctx = context.copy()

        for seance in self.browse(cr, uid, ids, context=context):
            if not seance.manual:
                proxy.create_procurements(cr, uid, [x.id for x in seance.participant_ids], context=context)
            else:
                self.create_procurements(cr, uid, [seance.id], context=context)

            # send email to suppliers
            partners = set()
            for po_line in seance.purchase_line_ids:
                for seller in po_line.product_id.seller_ids:
                    partners.add(seller.name)


            for partner in partners:
                to = None
                for address in partner.address:
                    if not address.email:
                        continue
                    if address.type == 'delivery':
                        to = address.email
                        break
                    elif address.type == 'default':
                        to = address.email

                if to is None:
                    continue

                report_ctx['partner'] = partner
                pdf, _ = report.create(cr, uid, [seance.id], {}, context=report_ctx)
                filename = seance.name.replace('/', ' ') + '.pdf'
                emails.send_email(cr, uid, 'procurements', 's', to=to, attachments=[(filename, pdf),], context=context, seance=seance, partner=partner)

        return self.write(cr, uid, ids, {'state' : 'confirmed'}, context=context)

    # training.seance
    def action_workflow_inprogress(self, cr, uid, ids, context=None):
        workflow = netsvc.LocalService('workflow')

        for seance in self.browse(cr, uid, ids, context=context):
            for session in seance.session_ids:
                workflow.trg_validate(uid, 'training.session', session.id, 'signal_inprogress', cr)

        return self.write(cr, uid, ids, {'state' : 'inprogress'}, context=context)

    # training.seance
    def action_workflow_close(self, cr, uid, ids, context=None):
        return self.write(cr, uid, ids, {'state' : 'closed'}, context=context)

    # training.seance
    def test_workflow_done(self, cr, uid, ids, context=None):
        return True

    # training.seance
    def action_workflow_done(self, cr, uid, ids, context=None):
        workflow = netsvc.LocalService('workflow')
        self.write(cr, uid, ids, {'state' : 'done'}, context=context)

        for seance in self.browse(cr, uid, ids, context=context):
            for participation in seance.contact_ids:
                workflow.trg_validate(uid, 'training.participation.stakeholder', participation.id, 'signal_done', cr)
            for session in seance.session_ids:
                workflow.trg_validate(uid, 'training.session', session.id, 'signal_close', cr)

        return True

    # training.seance
    def test_workflow_cancel(self, cr, uid, ids, context=None):
        can_be_cancelled = any(session.state in ('cancelled', 'inprogress') for seance in self.browse(cr, uid, ids, context)
                               for session in seance.session_ids)
        return can_be_cancelled

    # training.seance
    def action_workflow_cancel(self, cr, uid, ids, context=None):
        workflow = netsvc.LocalService('workflow')

        # Annulation des commandes, des formateurs et des procurements
        mrp_products = {}
        part_ids = []
        for seance in self.browse(cr, uid, ids, context=context):

            for line in seance.purchase_line_ids:
                mrp_products[(seance.id, line.product_id.id,)] = line.fix

            for participation in seance.participant_ids:
                part_ids.append(participation.id)
                for purchase in participation.purchase_ids:
                    key = (seance.id, purchase.product_id.id,)
                    if purchase.state == 'confirmed' and key in mrp_products:
                        workflow.trg_validate(uid, 'purchase.order', purchase.order_id.id, 'purchase_cancel', cr)

            for participation in seance.contact_ids:
                ### if not participation.participation_sh_session_id:
                ###    # participation on this seance only...
                workflow.trg_validate(uid, 'training.participation.stakeholder', participation.id, 'signal_cancel', cr)

            for session in seance.session_ids:
                workflow.trg_validate(uid, 'training.session', session.id, 'signal_close', cr)

        self.pool.get('training.participation').unlink(cr, uid, part_ids, context=context)

        return self.write(cr, uid, ids, {'state' : 'cancelled'}, context=context)

    # training.seance
    def _create_participation(self, cr, uid, seance, subscription_line, context=None):
        proxy = self.pool.get('training.participation')
        values = {
            'seance_id' : seance.id,
            'subscription_line_id' : subscription_line.id,
        }
        return proxy.create(cr, uid, values, context=context)

    def on_change_date(self, cr, uid, ids, date, context=None):
        old_date = ids and self.browse(cr, uid, ids[0], context=context).date or 0

        if self.pool.get('training.holiday.period').is_in_period(cr, date):
            return {
                'value' : {
                    'date' : old_date,
                },
                'warning' : {
                    'title' : _("Selection Date"),
                    'message' : _("You can not select this date because it is a public holiday"),
                },
            }
        return {}

    # training.seance
    def create_procurements(self, cr, uid, ids, context=None):
        purchase_order_pool = self.pool.get('purchase.order')
        location_id = self.pool.get('stock.location').search(cr, uid, [('usage', '=', 'internal')], context=context)[0]

        for seance in self.browse(cr, uid, ids, context=context):
            if seance.manual:
                for po_line in seance.purchase_line_ids:
                    quantity = po_line.product_qty
                    if po_line.fix == 'by_subscription':
                        quantity = quantity * seance.participant_count_manual

                    procurement_id = purchase_order_pool.create_from_procurement_line(cr, uid, po_line, quantity, location_id, context=context)

        return True

    def unlink(self, cr, uid, ids, context=None):
        for seance in self.browse(cr, uid, ids, context=context):
            if seance.state == 'confirmed':
                for pl in seance.purchase_line_ids:
                    if pl.procurement_id:
                        raise osv.except_osv(_("Warning"),
                                             _("You can not suppress a seance with a confirmed procurement"))
            else:
                for participant in seance.participant_ids:
                    if participant.subscription_line_id.invoice_line_id:
                        raise osv.except_osv(_('Warning'),
                                             _("You can not suppress a seance with a invoiced subscription"))

        return super(training_seance, self).unlink(cr, uid, ids, context=context)

    def copy(self, cr, uid, object_id, values, context=None):
        if not 'is_first_seance' in values:
            values['is_first_seance'] = 0

        return super(training_seance, self).copy(cr, uid, object_id, values, context=context)

    def search(self, cr, uid, domain, offset=0, limit=None, order=None, context=None, count=False):
        offer_id = context and context.get('offer_id', False) or False

        if offer_id:
            date = context and context.get('date', False) or False
            if not date:
                date = time.strftime('%Y-%m-%d')
            cr.execute("SELECT seance.id AS seance_id, rel.offer_id, seance.course_id, seance.name, seance.state, seance.date "
                       "FROM training_seance seance, training_course_offer_rel rel "
                       "WHERE seance.course_id = rel.course_id "
                       "AND rel.offer_id = %s "
                       "AND seance.state = %s "
                       "AND seance.date >= %s "
                       "AND seance.duplicated = %s ",
                       (offer_id, 'opened', date, False,))

            return [x[0] for x in cr.fetchall()]

        job_id = context and context.get('job_id', False) or False
        request_session_id = context and context.get('request_session_id', False) or False

        if job_id and request_session_id:
            session = self.pool.get('training.session').browse(cr, uid, request_session_id, context=context)
            course_proxy = self.pool.get('training.course')
            seance_ids = []
            for seance in session.seance_ids:
                if seance.course_id and job_id:
                    cr.execute('select course_id from training_course_job_rel where course_id = %s and job_id = %s',
                               (seance.course_id.id, job_id,)
                              )

                    res = [x[0] for x in cr.fetchall()]
                    if res:
                        seance_ids.append(seance.id)

            return seance_ids


        return super(TrainingSeanse, self).search(cr, uid, domain, offset=offset,
                                                   limit=limit, order=order, context=context, count=count)

    def _get_product(self, cr, uid, ids, context=None):
        assert len(ids) == 1
        seance = self.browse(cr, uid, ids[0], context)
        return seance.course_id.course_type_id.product_id