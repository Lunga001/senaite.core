# -*- coding: utf-8 -*-
#
# This file is part of SENAITE.CORE
#
# Copyright 2018 by it's authors.
# Some rights reserved. See LICENSE.rst, CONTRIBUTORS.rst.

import json
import math
import plone

from zope.component import adapts
from zope.component import getAdapters
from zope.interface import implements

from Products.Archetypes.config import REFERENCE_CATALOG
from Products.CMFCore.utils import getToolByName
from Products.PythonScripts.standard import html_quote

from bika.lims import bikaMessageFactory as _
from bika.lims import logger
from bika.lims import api
from bika.lims.browser import BrowserView
from bika.lims.interfaces import IAnalysis
from bika.lims.interfaces import IFieldIcons
from bika.lims.utils import isnumber
from bika.lims.utils import t
from bika.lims.utils import resolve_unit
from bika.lims.utils.analysis import format_numeric_result


class CalculationResultAlerts(object):
    """This uses IAnalysis.ResultOutOfRange on values in request.

    To validate results at ajax calculation time, make more adapters like this
    one, from IFieldIcons.  Any existing IAnalysis/IFieldIcon adapters
    (AnalysisOutOfRange) have already been called.
    """
    adapts(IAnalysis)
    implements(IFieldIcons)

    def __init__(self, context):
        self.context = context

    def __call__(self, result=None, specification=None, **kwargs):
        workflow = getToolByName(self.context, 'portal_workflow')
        astate = workflow.getInfoFor(self.context, 'review_state')
        if astate == 'retracted':
            return {}
        result = self.context.getResult() if result is None else result
        alerts = {}
        path = '++resource++bika.lims.images'
        uid = self.context.UID()
        try:
            indet = result.startswith("<") or result.startswith(">")
        except AttributeError:
            indet = False
        if indet:
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': t(_("Indeterminate result"))}
            if uid in alerts:
                alerts[uid].append(alert)
            else:
                alerts[uid] = [alert, ]
        return alerts


class ajaxCalculateAnalysisEntry(BrowserView):
    """This view is called by javascript when an analysis' result or interim
       field value is entered.
       Returns a JSON dictionary, or None if no action is required or possible.
    """

    def __init__(self, context, request):
        self.context = context
        self.request = request

    def extend_alerts(self, analysis_uid, alerts):
        """
        create or extend the alerts for the given analysis
        """
        if isinstance(alerts, basestring):
            alerts = list(alerts)

        logger.error('ajaxGetMethodCalculation: add alerts {}'.format(alerts))
        if analysis_uid in self.alerts:
            self.alerts[analysis_uid].extend(alert)
        else:
            self.alerts[analysis_uid] = alerts

    def combine_all_results(self):
        """
        Merge new results with the form results so calculations
        with calculations are processed
        """
        results = self.current_results
        for result in self.results:
            results[result['uid']]['result'] = result['result']
            results[result['uid']]['keyword'] = result['keyword']
        return results

    def get_analysis_value_by_keyword(self, keyword, results):
        """
        return the analysis dict for the given keyword
        """
        for key in results.keys():
            if not results[key].get('keyword', False):
                logger.error(
                    'get_analysis_value_by_keyword: no keyword found {}'.format(
                        str(results[key])))
                continue

            if results[key]['keyword'] == keyword:
                return (key, results[key])

    def get_interim_value_by_keyword(self, keyword, results):
        """
        return the interim dict for the given keyword
        """
        for key in results.keys():
            if len(results[key]) == 0:
                continue
            for interim in results[key]:
                if not interim.get('keyword', False):
                    continue
                if interim.get('keyword') == keyword:
                    return (interim['keyword'], interim['result'])
        raise api.BikaLIMSError(
            """get_interim_value_by_keyword: interim keyword {}
            not found""".format(keyword))

    def process_calculation(self, analysis, deps):
        """We need first to create the map of available parameters
           acording to the interims, analyses and wildcards:

         params = {
                <as-1-keyword>              : <analysis_result>,
                <as-1-keyword>.<wildcard-1> : <wildcard_1_value>,
                <as-1-keyword>.<wildcard-2> : <wildcard_2_value>,
                <interim-1>                 : <interim_result>,
                ...
                }
        """

        path = '++resource++bika.lims.images'
        mapping = {}
        analysis_uid = analysis.UID()
        calculation = analysis.getCalculation()

        logger.info(
            'ajaxGetMethodCalculation: process_calculation for {}'.format(
                calculation.Title()))

        # Get dependent analyses results and wildcard values to the
        # mapping. If dependent analysis without result found,
        # break and abort calculation
        unsatisfied = False
        for (dependency_uid, dependency) in deps:
            if dependency_uid in self.ignore_uids:
                unsatisfied = True
                break

            # LIMS-1769. Allow to use LDL and UDL in calculations.
            # https://jira.bikalabs.com/browse/LIMS-1769
            analysisvalues = {}
            if dependency_uid in self.current_results:
                analysisvalues = self.current_results[dependency_uid]
            else:
                # Retrieve the result and DLs from the analysis
                analysisvalues = {
                    'keyword': dependency.get('keyword'),
                    'result': dependency.get('results'),
                    'ldl': dependency.get('ldl'),
                    'udl': dependency.get('udl'),
                    'belowldl': dependency.get('belowldl'),
                    'aboveudl': dependency.get('aboveudl'),
                }
            if analysisvalues['result'] == '':
                unsatisfied = True
                break
            key = analysisvalues.get('keyword', dependency.get('keyword'))

            # Analysis result
            # All result mappings must be float, or they are ignored.
            try:
                mapping[key] = float(analysisvalues.get('result'))
                mapping['%s.%s' % (key, 'RESULT')] = float(
                    analysisvalues.get('result'))
                mapping['%s.%s' % (key, 'LDL')] = float(
                    analysisvalues.get('ldl'))
                mapping['%s.%s' % (key, 'UDL')] = float(
                    analysisvalues.get('udl'))
                mapping['%s.%s' % (key, 'BELOWLDL')] = int(
                    analysisvalues.get('belowldl'))
                mapping['%s.%s' % (key, 'ABOVEUDL')] = int(
                    analysisvalues.get('aboveudl'))
            except:
                # If not floatable, then abort!
                unsatisfied = True
                break

        if unsatisfied:
            # unsatisfied means that one or more result on which we depend
            # is blank or unavailable. this should never happen
            raise api.BikaLIMSError(
                'ajaxGetMethodCalculation: no analysis should be unsatisfied')

        # convert formula to a valid python string, ready for interpolation
        formula = calculation.getMinifiedFormula()
        formula = formula.replace('[', '%(').replace(']', ')f')
        try:
            formula = eval("'%s'%%mapping" % formula,
                           {"__builtins__": None,
                            'math': math,
                            'context': self.context},
                           {'mapping': mapping})
            # calculate
            result = eval(formula, calculation._getGlobals())
            self.current_results[analysis_uid]['result'] = result

        except TypeError as e:
            # non-numeric arguments in interim mapping?
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': "{0}: {1} ({2}) ".format(
                         t(_("Type Error")),
                         html_quote(str(e.args[0])),
                         formula)}
            self.extend_alerts(analysis_uid, alert)
        except ZeroDivisionError as e:
            Result['result'] = '0/0'
            Result['formatted_result'] = '0/0'
            self.results.append(Result)
            self.current_results[analysis_uid]['result'] = '0/0'
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': "{0}: {1} ({2}) ".format(
                         t(_("Division by zero")),
                         html_quote(str(e.args[0])),
                         formula)}
            self.extend_alerts(analysis_uid, alert)
            return False
        except KeyError as e:
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': "{0}: {1} ({2}) ".format(
                         t(_("Key Error")),
                         html_quote(str(e.args[0])),
                         formula)}
            self.extend_alerts(analysis_uid, alert)

        Result = {'uid': analysis_uid, 'result': result}
        self.process_analysis_result(analysis, Result)
        return True

    def process_interims(self, analysis, deps):
        """First to create the map of available parameters
           acording to the interims and then use them in the formula
        """

        path = '++resource++bika.lims.images'
        mapping = {}
        analysis_uid = analysis.UID()
        calculation = analysis.getCalculation()

        logger.info(
            'ajaxGetMethodCalculation: process_interims for {}'.format(
                calculation.Title()))

        # Add all interims to mapping
        for dep in deps:
            try:
                mapping[dep[0]] = float(dep[1])
            except TypeError as e:
                alert = {'field': 'Result',
                         'icon': path + '/exclamation.png',
                         'msg': "{0}: {1}".format(
                             t(_("Type Error")),
                             html_quote(str(e.args[0])),
                             dep[1])}
                self.extend_alerts(analysis_uid, alert)
                return False

        # convert formula to a valid python string, ready for interpolation
        formula = calculation.getMinifiedFormula()
        formula = formula.replace('[', '%(').replace(']', ')f')
        try:
            formula = eval("'%s'%%mapping" % formula,
                           {"__builtins__": None,
                            'math': math,
                            'context': self.context},
                           {'mapping': mapping})
            # calculate
            result = eval(formula, calculation._getGlobals())
            self.current_results[analysis_uid]['result'] = result

        except TypeError as e:
            # non-numeric arguments in interim mapping?
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': "{0}: {1} ({2}) ".format(
                         t(_("Type Error")),
                         html_quote(str(e.args[0])),
                         formula)}
            self.extend_alerts(analysis_uid, alert)
            return False
        except ZeroDivisionError as e:
            Result['result'] = '0/0'
            Result['formatted_result'] = '0/0'
            self.results.append(Result)
            self.current_results[analysis_uid]['result'] = '0/0'
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': "{0}: {1} ({2}) ".format(
                         t(_("Division by zero")),
                         html_quote(str(e.args[0])),
                         formula)}
            self.extend_alerts(analysis_uid, alert)
            return False
        except KeyError as e:
            alert = {'field': 'Result',
                     'icon': path + '/exclamation.png',
                     'msg': "{0}: {1} ({2}) ".format(
                         t(_("Key Error")),
                         html_quote(str(e.args[0])),
                         formula)}
            self.extend_alerts(analysis_uid, alert)

        Result = {'uid': analysis_uid, 'result': result}
        self.process_analysis_result(analysis, Result)
        return True

    def process_analysis_result(self, analysis, Result):
        analysis_uid = analysis.UID()

        # Convert Unit
        if Result['result']:
            Result['converted_result'] = \
                resolve_unit(analysis, Result['result'])

        # format result
        try:
            Result['formatted_result'] = format_numeric_result(
                analysis, Result['result'])
        except ValueError:
            # non-float
            Result['formatted_result'] = Result['result']

        # calculate Dry Matter result
        # if parent is not an AR, it's never going to be calculable
        dm = hasattr(analysis.aq_parent, 'getReportDryMatter') and \
            analysis.aq_parent.getReportDryMatter() and \
            analysis.getReportDryMatter()

        if dm:
            dry_service = self.context.bika_setup.getDryMatterService()
            # get the UID of the DryMatter Analysis from our parent AR
            dry_analysis = [a for a in
                            analysis.aq_parent.getAnalyses(full_objects=True)
                            if a.getServiceUID() == dry_service.UID()]
            if dry_analysis:
                dry_analysis = dry_analysis[0]
                dry_uid = dry_analysis.UID()
                # get the current DryMatter analysis result from the form
                if dry_uid in self.current_results:
                    try:
                        dry_result = float(self.current_results[dry_uid])
                    except:
                        dm = False
                else:
                    try:
                        dry_result = float(dry_analysis.getResult())
                    except:
                        dm = False
            else:
                dm = False
        Result['dry_result'] = dm and dry_result and \
            '%.2f' % ((Result['result'] / dry_result) * 100) or ''

        Result['keyword'] = analysis.getKeyword()
        self.results.append(Result)

        # LIMS-1808 Uncertainty calculation on DL
        # https://jira.bikalabs.com/browse/LIMS-1808
        flres = Result.get('result', None)
        if flres and isnumber(flres):
            flres = float(flres)
            anvals = self.current_results[analysis_uid]
            isldl = anvals.get('isldl', False)
            isudl = anvals.get('isudl', False)
            ldl = anvals.get('ldl', 0)
            udl = anvals.get('udl', 0)
            ldl = float(ldl) if isnumber(ldl) else 0
            udl = float(udl) if isnumber(udl) else 10000000
            belowldl = (isldl or flres < ldl)
            aboveudl = (isudl or flres > udl)

            unc = '' if (belowldl or aboveudl) \
                else analysis.getUncertainty(Result.get('result'))
            if not (belowldl or aboveudl):
                analysis.getUncertainty(Result.get('result'))
                self.uncertainties.append(
                    {'uid': analysis_uid, 'uncertainty': unc})

        # These self.alerts are just for the json return.
        # we're placing the entire form's results in kwargs.
        adapters = getAdapters((analysis, ), IFieldIcons)
        for name, adapter in adapters:
            alerts = adapter(result=Result['result'],
                             form_results=self.current_results)
            if alerts:
                self.extend_alerts(analysis_uid, alerts)

    def calculation_contains_a_calculation(self, calc):
        """
        Return true if a calculation contains a calculation
        """
        for svc in calc.getDependentServices():
            if svc.getCalculation():
                return True
        return False

    def calculate(self, analysis_uid=None):
        analysis = self.analyses[analysis_uid]

        # process form_result if not a calculation
        form_result = self.current_results[analysis_uid]['result']
        Result = {'uid': analysis_uid, 'result': form_result}
        self.process_analysis_result(analysis, Result)

        # Get all analyses that are calculations
        calc_ans = [
            self.analyses[uid] for uid in
            filter(
                lambda x: self.analyses[x].getCalculation(),
                self.analyses)]

        # Ensure calculation that contain calculation are processed last
        calc_ans.sort(
            key=lambda x: self.calculation_contains_a_calculation(
                x.getCalculation()),
            reverse=False)

        # process calculations that have all required results
        for calc_an in calc_ans:
            logger.info(
                'ajaxGetMethodCalculation: updatable {}?'.format(
                    calc_an.Title()))

            # get updated result set
            results = self.combine_all_results()

            dep_svcs = calc_an.getCalculation().getDependentServices()
            if len(dep_svcs) > 0:
                # gather dependent analysis for performance
                deps = []

                # break if any dep has no results
                missing_results = False
                for svc in dep_svcs:
                    (an_uid, result) = \
                        self.get_analysis_value_by_keyword(svc.getKeyword(), results)
                    deps.append((an_uid, result))
                    if len(str(result['result'])) == 0:
                        missing_results = True
                        break
                if not missing_results:
                    self.process_calculation(calc_an, deps)

            interims = calc_an.getCalculation().getInterimFields()
            if len(interims) > 0:
                # gather dependent analysis for performance
                deps = []

                # break if any dep has no results
                missing_results = False
                for interim in interims:
                    (keyword, result) = self.get_interim_value_by_keyword(
                        interim['keyword'], self.current_interims)
                    deps.append((keyword, result))
                    if result == "0.0":
                        missing_results = True
                        break
                if not missing_results:
                    self.process_interims(calc_an, deps)

        return True

    def __call__(self):
        self.rc = getToolByName(self.context, REFERENCE_CATALOG)
        plone.protect.CheckAuthenticator(self.request)
        plone.protect.PostOnly(self.request)

        self.spec = self.request.get('specification', None)

        # information about the triggering element
        uid = self.request.get('uid')
        self.field = self.request.get('field')
        self.value = self.request.get('value')

        self.current_results = json.loads(self.request.get('results'))
        self.current_interims = self.request.get('interims')
        self.current_interims = json.loads(self.request.get('interims'))
        form_results = json.loads(self.request.get('results'))
        self.item_data = json.loads(self.request.get('item_data'))

        # these get sent back the the javascript
        self.alerts = {}
        self.uncertainties = []
        self.results = []

        self.services = {}
        self.analyses = {}
        # ignore these analyses if objects no longer exist
        self.ignore_uids = []

        for analysis_uid, result in self.current_results.items():
            analysis = self.rc.lookupObject(analysis_uid)
            if not analysis:
                self.ignore_uids.append(analysis_uid)
                continue
            self.analyses[analysis_uid] = analysis

        if uid not in self.ignore_uids:
            self.calculate(uid)

        results = []
        for result in self.results:
            if result['uid'] in form_results.keys() and \
               result['result'] != form_results[result['uid']]:
                results.append(result)

        return json.dumps({'alerts': self.alerts,
                           'uncertainties': self.uncertainties,
                           'results': results})


class ajaxGetMethodCalculation(BrowserView):
    """ Returns the calculation assigned to the defined method.
        uid: unique identifier of the method
    """
    def __call__(self):
        plone.protect.CheckAuthenticator(self.request)
        calcdict = {}
        uc = getToolByName(self, 'uid_catalog')
        method = uc(UID=self.request.get("uid", '0'))
        if method and len(method) == 1:
            calc = method[0].getObject().getCalculation()
            if calc:
                calcdict = {'uid': calc.UID(),
                            'title': calc.Title()}
        return json.dumps(calcdict)


class ajaxGetAvailableCalculations(BrowserView):
    """
    Returns all available calculations.
    """
    def __call__(self):
        plone.protect.CheckAuthenticator(self.request)

        bsc = getToolByName(self, 'bika_setup_catalog')
        items = [(i.UID, i.Title)
                 for i in bsc(portal_type='Calculation',
                              inactive_state='active')]
        items.sort(lambda x, y: cmp(x[1], y[1]))
        items.insert(0, ('', _("None")))
        calcdict = [{'uid': calc[0], 'title': calc[1]} for calc in items]

        return json.dumps(calcdict)
