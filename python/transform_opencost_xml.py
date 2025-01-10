#!/usr/bin/env python3
# -*- coding: UTF-8 -*-

import argparse
import csv
import logging
import os
import sys

import openapc_toolkit as oat

TARGET_DIR = "opencost_out"
TA_FILE = "../data/transformative_agreements/transformative_agreements.csv"
WILEY_OPT_OUT_FILE = "../data/transformative_agreements/deal_germany_opt_out/deal_wiley_germany_opt_out.csv"
SPRINGER_OPT_OUT_FILE = "../data/transformative_agreements/deal_germany_opt_out/deal_springer_nature_germany_opt_out.csv"

ROR_MAP = {}

INS_STR = {
    "sn2020deal": "DEAL Springer Nature Germany",
    "wiley2019deal": "DEAL Wiley Germany",
    "els2023deal": "DEAL Elsevier Germany",
    "ov_hdr": "=== Overview: Contract group IDs ===\n\n",
    "ov_report": "{} has reported {} cost data for the {} period, total costs: {} €. (group_id: {})\n",
    "ov_merge_note": "NOTE: This group_id was merged during processing, meaning that it occured more than once.\n",
    "ov_dup_warning": "WARNING: There were invoice groups which are completely identical in total costs. A duplicate case is likely.\n",
    "ov_articles": "{} new articles without publication-level costs were linked to this group id. A preliminary EAPC of {} / {} = {} was assigned to each of them.\nCONTROL: First new article in group ({}) has an 'euro' value of {} €. ==> {}.\n",
    "ov_no_articles": "There were no new articles without publication-level costs linked to this group id.\n",
    "ov_ta_data": "NOTE: The institution has already reported TA DEAL data for the given period and publisher ({} articles, EAPC: {} €).",
    "ov_ta_update_maybe": " The EAPC might need an update, see below.\n",
    "ov_ta_update_necessary": " The EAPC needs to be updated, see below.\n",
    "ov_other_group": "NOTE: There is another invoice group with cost data for the given institution, period and publisher.\n",
    "ins_hdr": "=== Update instructions ===\n\n",
    "ins_clear": "There is no conflicting cost data for invoice group '{}', the calculated EAPC is valid.\n ==> The data file ({}, {}, {}) can be directly enriched and processed.\n\n",
    "ins_no_articles": "There are no articles linked to the cost data in all invoice groups related to {} {} and no matching TA data exists.\n ==> Nothing to do here.\n\n",
    "ins_solitary_articles": "There are {} new articles for the agreement period {} {}, but neither related cost data nor matching TA data exists.\n ==> Generated file should be deleted.\n\n",
    "ins_not_clear": "There are existing TA DEAL articles, additional invoice groups or duplicates with existing data for the agreement period {} {}.\n ==> Checking if EAPC needs to be updated.\n\n",
    "recalc_hdr": "  data_source                                            num_articles         total_costs (€)\n",
    "recalc_hbar": "  -------------------------------------------------------------------------------------------\n",
    "recalc_check_eapc": " (Total: {} , EAPC: {}, checking first TA article ({}) => {})\n",
    "recalc_res": "  New EAPC: {} / {} = {}\n\n",
    "recalc_update_ta": " ==> Existing DEAL data ({}, {}, {}) has to be updated with the new EAPC ({}) as euro value ({} articles in original file(s), enriched file(s) and TA collection).\n",
    "recalc_no_update_ta": " ==> EAPC for existing DEAL data ({}, {}, {}) is already correct ({}), no changes are needed.\n",
    "recalc_opt_out": " IMPORTANT: Please note that some articles are located in the {} opt-out file, those have to be updated as well.\n",
    "recalc_update_new_data": " ==> New DEAL data ({}, {}, {}) has to be updated with the new EAPC ({}) as euro value before enrichment ({} articles).\n",
    "recalc_all_duplicates": " ==> New DEAL data ({}, {}, {}) contains only duplicates. File is empty and should be deleted.\n",
    "duplicates_warn": "WARNING: Some of the removed duplicates had mismatches in period/institution:\nnew data --- old data\n",
}


def _is_internal_duplicate(article_dict_1, article_dict_2):
    for key in ["institution", "period"]:
        if article_dict_1[key] != article_dict_2[key]:
            return False
    return True

"""
Create institutional DEAL update instructions

The openCost toolkit calculates a preliminary EAPC by distributing contract invoice costs over all
0€-articles which are linked to it via a group_id. In an optimal case, this creates a complete and final
EAPC distribution for a given institution/period/agreement combination ("IPA combo").

Unfortunately there are a number of cases were this calculation is invalid and has to be corrected afterwards:

- There's already existing TA for the IPA combo, so the numbers have to be updated.
- There are new DEAL articles for an IPA combo which are not linked to an invoice group (but have a DEAL ESAC ID)
- New DEAL articles form duplicates with TA data and have to be discounted
- Invoice groups without linked articles (submitted corrections/updates to previous cost data)

This function takes all these factors into account and creates update instruction files on an
institutional level. It also deletes detected duplicates from the created DEAL input files.

"""

def prepare_deal_update(args, ta_data, ta_doi_lookup, new_deal_writers, invoice_groups):
    deal_map = {}
    invoice_group_dict = {}
    instructions = {}
    # Create a unified lookup structure. It contains all institution/period/agreement combos for which either at least one invoice group or new deal articles exist (or both).
    # Enrich the structure with possible existing old TA data afterwards.
    for group in invoice_groups:
        if group["contract_id"] not in ["sn2020deal", "wiley2019deal", "els2023deal"]:
            continue
        group_id = group["group_id"]
        if group_id not in invoice_group_dict:
            invoice_group_dict[group_id] = []
        ror_id = group["institution_ror"]
        ins_name = ROR_MAP[ror_id]
        period = group["period"]
        agreement = INS_STR[group["contract_id"]]
        total_cost = group["group_total_costs"]
        if ins_name not in deal_map:
            deal_map[ins_name] = {}
        if period not in deal_map[ins_name]:
            deal_map[ins_name][period] = {}
        if agreement not in deal_map[ins_name][period]:
            deal_map[ins_name][period][agreement] = {
                "groups": [],
                "writer_data": None,
                "ta_data": ta_data.get(ins_name, {}).get(period, {}).get(agreement, None)
            }
        deal_map[ins_name][period][agreement]["groups"].append(group)
    for ins_name, ins_data in new_deal_writers.items():
        if ins_name not in deal_map:
            deal_map[ins_name] = {}
        for period, period_data in ins_data.items():
            if period not in deal_map[ins_name]:
                deal_map[ins_name][period] = {}
            for agreement, writer_data in period_data.items():
                if agreement not in deal_map[ins_name][period]:
                    deal_map[ins_name][period][agreement] = {
                        "groups": [],
                        "writer_data": writer_data,
                        "ta_data": ta_data.get(ins_name, {}).get(period, {}).get(agreement, None)
                    }
                deal_map[ins_name][period][agreement]["writer_data"] = writer_data
                for article in writer_data["articles"]:
                    group_id = article["contract_group_id"]
                    if group_id == "NA":
                        continue
                    if group_id not in invoice_group_dict:
                        invoice_group_dict[group_id] = []
                    invoice_group_dict[group_id].append(article)
    # Report on all invoice groups and linked new articles
    for ins_name, periods in deal_map.items():
        if ins_name not in instructions:
            instructions[ins_name] = INS_STR["ov_hdr"]
        for period, agreements in periods.items():
            for agreement, data in agreements.items():
                for group in data["groups"]:
                    group_id = group["group_id"]
                    total_cost = group["group_total_costs"]
                    ins_txt = INS_STR["ov_report"].format(ins_name, INS_STR[group["contract_id"]], period, total_cost, group_id)
                    if group["merged"]:
                        ins_txt += INS_STR["ov_merge_note"]
                    if group["possible_duplicate"]:
                        ins_txt += INS_STR["ov_dup_warning"]
                    articles = invoice_group_dict.get(group_id, [])
                    if len(articles) > 0:
                        eapc = round(float(total_cost) / len(articles), 2)
                        passed = "passed" if int(eapc) == int(articles[0]["euro"]) else "FAILED!"
                        ins_txt += INS_STR["ov_articles"].format(len(articles), total_cost, len(articles), eapc, articles[0]["doi"], articles[0]["euro"], passed)
                    else:
                        ins_txt += INS_STR["ov_no_articles"]
                    if data["ta_data"] is not None:
                        old_articles = data["ta_data"]["articles"]
                        if len(old_articles) > 0:
                            old_eapc = float(old_articles[0]["euro"])
                            ins_txt += INS_STR["ov_ta_data"].format(len(old_articles), old_eapc)
                            if len(articles) > 0:
                                ins_txt += INS_STR["ov_ta_update_necessary"]
                            else:
                                ins_txt += INS_STR["ov_ta_update_maybe"]
                    if len(data["groups"]) > 1:
                        ins_txt += INS_STR["ov_other_group"]
                    instructions[ins_name] += ins_txt + "\n"
    # Create update instructions
    for ins_name, periods in deal_map.items():
        instructions[ins_name] += INS_STR["ins_hdr"]
        ins_index = 0
        for period, agreements in periods.items():
            for agreement, data in agreements.items():
                # sum up data and generate preliminary EAPC Recalculation text
                ins_index += 1
                total_costs = 0.0
                num_total_articles = 0
                num_ta_articles = 0
                num_new_articles = 0
                num_duplicates = 0
                non_internal_duplicates = False
                calc_str = INS_STR["ins_not_clear"].format(agreement, period)
                calc_str += " = Recalculation = \n" + INS_STR["recalc_hdr"] + INS_STR["recalc_hbar"]
                duplicates_str = INS_STR["duplicates_warn"]
                for group in data["groups"]:
                    total_costs += group["group_total_costs"]
                    calc_str += "  " + group["group_id"].ljust(55) + "--".ljust(20) + str(group["group_total_costs"]) + "\n"
                if data["ta_data"] is not None:
                    ta_articles = data["ta_data"]["articles"]
                    num_ta_articles = len(ta_articles)
                    num_total_articles += num_ta_articles
                    ta_total_costs = sum([float(article["euro"]) for article in ta_articles])
                    ta_eapc = round(ta_total_costs / num_ta_articles, 2)
                    calc_str += "  " + "TA data".ljust(55) + str(num_ta_articles).ljust(20) + "--   "
                    passed_str = "passed" if float(ta_articles[0]["euro"]) == ta_eapc else "FAILED!"
                    calc_str += INS_STR["recalc_check_eapc"].format(ta_total_costs, ta_eapc, ta_articles[0]["doi"], passed_str)
                if data["writer_data"] is not None:
                    new_articles = data["writer_data"]["articles"]
                    num_new_articles = len(new_articles)
                    num_total_articles += num_new_articles
                    calc_str += "  " + "New OAPK articles".ljust(55) + str(num_new_articles).ljust(20) + "--" + "\n"
                    # Find and remove duplicates
                    for new_article in list(new_articles):
                        if new_article["doi"] in ta_doi_lookup:
                            ta_article = ta_doi_lookup[new_article["doi"]]
                            num_duplicates += 1
                            if not _is_internal_duplicate(new_article, ta_article):
                                non_internal_duplicates = True
                                msg = "Found a duplicate while preparing DEAL update data and the IPA metadata is not equal.\n  New article:    {}\n  TA article:      {}"
                                logging.warning(msg.format(new_article, ta_article))
                                dup_line = "{}, {}, {}  ---- {}, {}, {}\n"
                                dup_line = dup_line.format(new_article["doi"], new_article["institution"], new_article["period"], ta_article["doi"], ta_article["institution"], ta_article["period"])
                                duplicates_str += dup_line
                            else:
                                msg = "Found an internal duplicate while preparing DEAL update data.\n  New article:    {}\n  TA article:      {}"
                                logging.warning(msg.format(new_article, ta_article))
                            for key, value in new_article.items():
                                new_article[key] = ""
                    if num_duplicates > 0:
                        num_total_articles -= num_duplicates
                        num_new_articles -= num_duplicates
                        calc_str += "  " + "Duplicates".ljust(55) + "-" + str(num_duplicates).ljust(19) + "--" + "\n"
                calc_str += INS_STR["recalc_hbar"]
                calc_str += "  " + "Sum".ljust(55) + str(num_total_articles).ljust(20) + str(total_costs) + "\n\n"
                if num_total_articles > 0:
                    new_eapc = round(total_costs / num_total_articles, 2)
                    calc_str += INS_STR["recalc_res"].format(total_costs, num_total_articles, new_eapc)
                    if num_ta_articles > 0:
                        old_eapc = round(ta_total_costs / num_ta_articles, 2)
                        if old_eapc != new_eapc:
                            calc_str += INS_STR["recalc_update_ta"].format(ins_name, agreement, period, new_eapc, num_ta_articles)
                            if data["ta_data"]["opt_out_articles"]:
                                calc_str += INS_STR["recalc_opt_out"].format(agreement)
                        else:
                            calc_str += INS_STR["recalc_no_update_ta"].format(ins_name, agreement, period, old_eapc)
                    if num_new_articles > 0:
                        calc_str += INS_STR["recalc_update_new_data"].format(ins_name, agreement, period, new_eapc, num_new_articles - num_duplicates)
                    elif num_duplicates > 0:
                        calc_str += INS_STR["recalc_all_duplicates"].format(ins_name, agreement, period, new_eapc)
                    calc_str += "\n"
                    if non_internal_duplicates:
                        calc_str += duplicates_str + "\n"
                instructions[ins_name] += str(ins_index) + ") "
                # Decide which type of instruction to show
                if num_total_articles == 0:
                    instructions[ins_name] += INS_STR["ins_no_articles"].format(agreement, period)
                elif len(data["groups"]) == 0 and data["ta_data"] is None:
                    instructions[ins_name] += INS_STR["ins_solitary_articles"].format(num_total_articles, agreement, period)
                elif len(data["groups"]) == 1 and data["ta_data"] is None and num_duplicates == 0:
                    instructions[ins_name] += INS_STR["ins_clear"].format(data["groups"][0]["group_id"], ins_name, agreement, period)
                else:
                    instructions[ins_name] += calc_str
    
    for ins_name, ins_txt in instructions.items():
        ins_file_name = ins_name.lower().replace(" ", "_")
        ins_file_name += "_DEAL_calculations.txt"
        with open(os.path.join(TARGET_DIR, args.prefix + ins_file_name), "w") as handle:
            handle.write(ins_txt)

parser = argparse.ArgumentParser()
parser.add_argument("xml_files", nargs="+", help="One or more openCost XML files which should validate against the official openCost XSD schema")
parser.add_argument("-v", "--version", choices=["v1", "v2"], default="v1")
parser.add_argument("-p", "--prefix", default="", help="An optional prefix for generated file names")
parser.add_argument("-l", "--log_level", choices=["DEBUG", "INFO", "WARNING", "ERROR"], default="INFO", help="Set the log level of the underlying openCost toolkit library")
parser.add_argument("-m", "--merge_groups", action="store_true", help="Merge invoice groups with identical group_id by adding their total costs")

args = parser.parse_args()

if args.version == "v1":
    import opencost_toolkit as octk
else:
    import opencost_toolkit_v2 as octk

xml_content_strings = []

for path in args.xml_files:
    if os.path.isfile(path):
        with open(path) as f:
            content = f.read()
            xml_content_strings.append(content)
            
handler = logging.StreamHandler(sys.stderr)
handler.setFormatter(oat.ANSIColorFormatter())
bufferedHandler = oat.BufferedErrorHandler(handler)
bufferedHandler.setFormatter(oat.ANSIColorFormatter())
fileHandler = logging.FileHandler("oapk_transform_" + args.log_level.lower() + ".log")
logging.root.addHandler(handler)
logging.root.addHandler(bufferedHandler)
logging.root.addHandler(fileHandler)
logging.root.setLevel(args.log_level)
logging.debug("Logger initialized.")

articles, invoice_groups = octk.process_opencost_xml(*xml_content_strings)
invoice_groups = octk.detect_invoice_group_duplicates(invoice_groups)
if args.merge_groups:
    invoice_groups = octk.merge_invoice_groups(invoice_groups)
articles = octk.apply_contract_data(articles, invoice_groups)

with open(oat.INSTITUTIONS_FILE, "r") as ins_file:
    reader = csv.DictReader(ins_file)
    for line in reader:
        ROR_MAP[line["ror_id"]] = line["institution"]

for collection in [articles, invoice_groups]:
    for element in collection:
        ror_id = element["institution_ror"]
        new_ror_msg = "Encountered a ROR ID ({}) which is not present in the institutions table. {}"
        if oat.has_value(ror_id) and ror_id not in ROR_MAP:
            ror_request = oat.get_metadata_from_ror(ror_id)
            if ror_request["success"]:
                institution = ror_request["data"]["institution"]
                ROR_MAP[ror_id] = institution
                msg = new_ror_msg.format(ror_id, "It belongs to this institution: ")
                msg += institution
                logging.info(msg)
            else:
                msg = new_ror_msg.format(ror_id, "A ROR lookup failed: ")
                msg += ror_request["error_msg"]
                logging.error(msg)
        element["institution"] = ROR_MAP.get(ror_id, "")

fieldnames = list(octk.OPENCOST_EXTRACTION_FIELDS.keys())

if not os.path.isdir(TARGET_DIR):
    os.mkdir(TARGET_DIR)

csv_writers = {}

all_articles_path = os.path.join(TARGET_DIR, "all_articles.csv")

with open(all_articles_path, "w") as out:
    main_writer = csv.DictWriter(out, fieldnames, extrasaction="ignore")
    main_writer.writeheader()
    for article in articles:
        main_writer.writerow(article)
        institution = article["institution"]
        esac_id = article["contract_primary_identifier"]
        pub_type = article["type"]
        ins_name = institution.lower().replace(" ", "_")
        period = article["period"]
        deal_data = None
        if pub_type == "book":
            ins_name += "_BPC"
        elif pub_type in ["journal article", "other"]:
            # "other" might designate article types like "letter to the editor"
            if article["is_hybrid"] == "TRUE":
                # Skip hybrid DEAL articles with publication-level costs
                if esac_id in ["sn2020deal", "wiley2019deal", "els2023deal"]:
                    try:
                        euro_value = float(article["euro"])
                        if euro_value > 0.0:
                            msg = 'Found a hybrid DEAL article with publication level costs not linked to an invoice group ({}, {}, {}, {} €))'
                            logging.warning(msg.format(article["institution"], article["period"], article["doi"], article["euro"]))
                            continue
                    except ValueError:
                        pass
                    article["euro"] = article.get("contract_euro", 0)
                if esac_id == "sn2020deal":
                    ins_name += "_DEAL_Springer_" + period
                    deal_data = {
                        "period": period,
                        "institution": institution,
                        "agreement": "DEAL Springer Nature Germany"
                    }
                elif esac_id == "wiley2019deal":
                    ins_name += "_DEAL_Wiley_" + period
                    deal_data = {
                        "period": period,
                        "institution": institution,
                        "agreement": "DEAL Wiley Germany"
                    }
                elif esac_id == "els2023deal":
                    ins_name += "_DEAL_Elsevier_" + period
                    deal_data = {
                        "period": period,
                        "institution": institution,
                        "agreement": "DEAL Elsevier Germany"
                    }
                elif oat.has_value(esac_id):
                    # Skip contracts other than DEAL
                    continue
        else:
            # Skip other pub types
            continue
        if ins_name not in csv_writers:
            path = os.path.join(TARGET_DIR, args.prefix + ins_name + ".csv")
            handle = open(path, "w")
            csv_writers[ins_name] = {
                "writer": csv.DictWriter(handle, fieldnames, extrasaction="ignore"),
                "handle": handle,
                "articles": [],
                "deal_data": deal_data
            }
            csv_writers[ins_name]["writer"].writeheader()
        csv_writers[ins_name]["articles"].append(article)

ta_deal_data = {}
ta_doi_lookup = {}
# Extract existing DEAL data and create nested lookup dicts
for source in [TA_FILE, WILEY_OPT_OUT_FILE, SPRINGER_OPT_OUT_FILE]:
    with open(source, "r") as ta_file:
        reader = csv.DictReader(ta_file)
        for line in reader:
            institution = line["institution"]
            doi = line["doi"]
            agreement = line["agreement"]
            ta_doi_lookup[doi] = line
            if line["is_hybrid"] != "TRUE":
                continue
            if agreement not in ["DEAL Springer Nature Germany", "DEAL Wiley Germany", "DEAL Elsevier Germany"]:
                continue
            period = str(line["period"])
            if institution not in ta_deal_data:
                ta_deal_data[institution] = {}
            if period not in ta_deal_data[institution]:
                ta_deal_data[institution][period] = {}
            if agreement not in ta_deal_data[institution][period]:
                ta_deal_data[institution][period][agreement] = {
                    "articles": [],
                    "opt_out_articles": False,
                }
            ta_deal_data[institution][period][agreement]["articles"].append(line)
            if source in [WILEY_OPT_OUT_FILE, SPRINGER_OPT_OUT_FILE]:
                ta_deal_data[institution][period][agreement]["opt_out_articles"] = True

new_deal_writers = {}
# Also create a nested lookup dict for csv writers with DEAL data
for _, data in csv_writers.items():
    if data["deal_data"] is None:
        continue
    ins = data["deal_data"]["institution"]
    period = data["deal_data"]["period"]
    agreement = data["deal_data"]["agreement"]
    if ins not in new_deal_writers:
        new_deal_writers[ins] = {}
    if period not in new_deal_writers[ins]:
        new_deal_writers[ins][period] = {}
    if agreement not in new_deal_writers[ins][period]:
        new_deal_writers[ins][period][agreement] = data

prepare_deal_update(args, ta_deal_data, ta_doi_lookup, new_deal_writers, invoice_groups)

# Create institutional output files
for writer_name, writer_data in csv_writers.items():
    for article in writer_data["articles"]:
        writer_data["writer"].writerow(article)
    writer_data["handle"].close()

# Flush buffered log messages
bufferedHandler.close()
